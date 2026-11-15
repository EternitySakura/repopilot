from fixture_text import parse_words


def test_comma_separated_words():
    assert parse_words("alpha,beta") == ["alpha", "beta"]


def test_whitespace_and_blank_fields():
    assert parse_words(" alpha, , beta ") == ["alpha", "beta"]


def test_empty_input():
    assert parse_words("") == []
