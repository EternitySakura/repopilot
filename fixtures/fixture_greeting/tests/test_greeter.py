from importlib import import_module

from fixture_greeting import greet


def test_plain_name():
    assert greet("Ada") == "Hello, Ada!"


def test_unicode_name():
    assert greet("李") == "Hello, 李!"


def test_whitespace_and_blank_name():
    assert greet(" Ada ") == "Hello, Ada!"
    assert greet(" \t ") == "Hello, Guest!"


def test_new_formatting_module():
    # Import during the call so the broken baseline still collects all tests.
    normalize_name = import_module("fixture_greeting.formatting").normalize_name
    assert normalize_name(" Ada ") == "Ada"
    assert normalize_name("") == "Guest"
