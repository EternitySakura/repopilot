import pytest
from fixture_records import parse_pair


def test_simple_pair():
    assert parse_pair("a=1") == ("a", "1")


def test_whitespace_and_empty_value():
    assert parse_pair(" key = value ") == ("key", "value")
    assert parse_pair("key= ") == ("key", "")


def test_value_contains_equals():
    assert parse_pair("key=a=b") == ("key", "a=b")


def test_missing_separator():
    with pytest.raises(ValueError):
        parse_pair("no separator")
