from fixture_paths import suffix


def test_extension():
    assert suffix("archive.tar.gz") == "gz"
    assert suffix("notes.txt") == "txt"
    assert suffix(".config.json") == "json"


def test_extensionless():
    assert suffix("README") == ""
    assert suffix("") == ""
    assert suffix("notes.") == ""


def test_dotfile_without_extension():
    assert suffix(".profile") == ""
