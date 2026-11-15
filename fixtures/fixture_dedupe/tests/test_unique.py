from fixture_dedupe import unique


def test_empty_and_single_value():
    assert unique([]) == []
    assert unique(["x", "x"]) == ["x"]


def test_preserve_first_order():
    assert unique([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_unhashable_values():
    assert unique([[1], [2], [1]]) == [[1], [2]]
    assert unique([{"a": 1}, {"a": 1}]) == [{"a": 1}]


def test_equal_values():
    assert unique([1, True]) == [1]
