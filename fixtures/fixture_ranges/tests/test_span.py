from fixture_ranges import covers


def test_interior_and_outside():
    assert covers(1, 3, 2)
    assert not covers(1, 3, 4)


def test_inclusive_endpoints():
    assert covers(1, 3, 1)
    assert covers(1, 3, 3)


def test_reversed_bounds():
    assert covers(3, 1, 2)
    assert covers(3, 1, 1)


def test_single_point():
    assert covers(2, 2, 2)
    assert not covers(2, 2, 1)
