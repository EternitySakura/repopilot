import json

import pytest
from pydantic import ValidationError

from repopilot.contracts.action import Action
from repopilot.contracts.tool_data import (
    ListDirectoryData,
    ReadFileData,
    SearchCodeData,
)


@pytest.mark.parametrize(("kind", "args"), [
    ("list_directory", {"path": ".", "depth": 2, "limit": 20}),
    ("search_code", {"path": "src", "query": "a.*[b]", "limit": 10}),
    ("read_file", {"path": "src/main.py", "start_line": 2, "end_line": 4}),
])
def test_action_round_trip(kind, args):
    action = Action.model_validate({"type": kind, "arguments": args})
    assert action.type == kind
    assert action.arguments.path == args["path"]
    assert json.loads(action.model_dump_json()) == {"type": kind, "arguments": args}
    assert Action.model_validate_json(action.model_dump_json()) == action


@pytest.mark.parametrize(("kind", "args"), [
    ("read_file", {"path": "src/a.py", "query": "foo"}),
    ("search_code", {"query": "foo", "start_line": 1}),
    ("list_directory", {"query": "foo"}),
    ("search_code", {"query": "foo", "literal": False}),
    ("search_code", {"query": ""}),
    ("search_code", {"query": "foo", "limit": 101}),
    ("list_directory", {"depth": 4}),
    ("list_directory", {"limit": 501}),
    ("read_file", {"path": "a.py", "start_line": 3, "end_line": 2}),
    ("read_file", {"path": "a.py", "start_line": 0}),
    ("list_directory", {"limit": "10"}),
    ("unknown", {}),
])
def test_invalid_actions(kind, args):
    with pytest.raises(ValidationError):
        Action.model_validate({"type": kind, "arguments": args})


@pytest.mark.parametrize("kind", ["read_file", "search_code", "list_directory"])
@pytest.mark.parametrize("path", ["/etc/passwd", "../secret", "src/../secret", "C:/a", "C:a", "src\\a", ""])
def test_actions_require_relative_paths(kind, path):
    args = {"path": path}
    if kind == "search_code":
        args["query"] = "text"
    with pytest.raises(ValidationError):
        Action.model_validate({"type": kind, "arguments": args})


def test_discriminator_schema():
    schema = Action.model_json_schema()
    assert schema["discriminator"]["propertyName"] == "type"
    assert set(schema["discriminator"]["mapping"]) == {
        "list_directory", "search_code", "read_file", "edit_file", "run_test", "read_test_result", "git_diff", "get_execution_status", "search_symbol", "apply_patch", "edit_files",
    }


def test_output_payloads():
    assert ListDirectoryData(entries=[]).truncated is False
    match = SearchCodeData(matches=[{"path": "src/a.py", "line": 2, "text": "a.*"}])
    assert match.matches[0].text == "a.*"
    data = ReadFileData(lines=[{"line": 2, "text": ""}], sha256="a" * 64)
    assert data.lines[0].line == 2
    with pytest.raises(ValidationError):
        ReadFileData(lines=[], sha256="invalid")
    with pytest.raises(ValidationError):
        SearchCodeData(matches=[{"path": "a.py", "line": 0, "text": "x"}])
