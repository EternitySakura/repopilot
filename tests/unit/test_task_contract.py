import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from repopilot.cli import main
from repopilot.contracts.action import Action
from repopilot.contracts.task import TaskSpec
from repopilot.contracts.tool_result import ToolResult
from repopilot.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


def task_data():
    return json.loads((ROOT / "tasks/rp-easy-001.json").read_text(encoding="utf-8"))


def test_agent_view_excludes_private_verifier():
    task = TaskSpec.model_validate(task_data())
    assert "verifier_profile" not in task.agent_view().model_dump()


@pytest.mark.parametrize("path", ["../tests/*", "src/../../tests/*", "C:/src/*", "src\\x"])
def test_task_rejects_unsafe_edit_scope(path):
    data = task_data()
    data["editable_globs"] = [path]
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(data)


def test_task_rejects_undeclared_fields():
    with pytest.raises(ValidationError):
        TaskSpec.model_validate({**task_data(), "api_key": "unexpected"})


def test_action_rejects_model_selected_identity():
    with pytest.raises(ValidationError):
        Action.model_validate({
            "type": "read_file", "arguments": {"path": "src/parser.py"}, "run_id": "other"
        })


def test_result_requires_consistent_status():
    with pytest.raises(ValidationError):
        ToolResult(tool_call_id=uuid4(), ok=False, workspace_generation=0, duration_ms=0)


def test_cli_summary_and_invalid_task(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.argv", ["repopilot", str(ROOT / "tasks/rp-easy-001.json")])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == "rp-easy-001"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["repopilot", str(invalid)])
    assert main() == 2
    assert "Invalid task" in capsys.readouterr().err


def test_settings_budget_validation(monkeypatch):
    monkeypatch.setenv("REPOPILOT_MAX_ITERATIONS", "0")
    with pytest.raises(ValidationError):
        Settings()
