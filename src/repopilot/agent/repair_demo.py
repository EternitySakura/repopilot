"""A scripted patch candidate and real public container tests, not a repair verdict."""

import traceback
from pathlib import Path

from repopilot.agent.loop import run_loop
from repopilot.agent.model_adapter import ModelAdapter
from repopilot.dispatcher import Dispatcher
from repopilot.observability.events import EventLog
from repopilot.observability.reports import RunTracker
from repopilot.repository.tasks import (
    DEFAULT_TASK_ID,
    PROJECT_ROOT,
    fixture_root,
    load_development_task,
)
from repopilot.settings import Settings
from repopilot.tools.edit import Editor
from repopilot.tools.policy import ToolFailure
from repopilot.tools.testing import DockerTests
from repopilot.workspace import Workspace


class RepairFakeModel:
    """Select edits using the real read hash; tool output is never precomputed."""

    def decide(self, observations):
        if not observations:
            return {"type": "read_file", "arguments": {"path": "src/fixture_text/parser.py"}}
        if not observations[-1].ok:
            return None
        if len(observations) == 1:
            return {"type": "edit_file", "arguments": {
                "path": "src/fixture_text/parser.py",
                "expected_sha256": observations[-1].data["sha256"],
                "old_text": '    if text[0] == ",":',
                "new_text": '    if text and text[0] == ",":',
            }}
        if len(observations) == 2:
            return {"type": "run_test", "arguments": {"profile": "target"}}
        return None


def run_repair_demo(runs: Path | None = None, *, settings: Settings | None = None,
                    model: ModelAdapter | None = None, task_id: str = DEFAULT_TASK_ID):
    config = settings if settings is not None else Settings()
    task = load_development_task(task_id)
    events = EventLog(runs if runs is not None else PROJECT_ROOT / "artifacts" / "runs")
    tracker = RunTracker(events, config, mode="scripted_candidate", task_id=task.task_id)
    workspace = None
    editor = None
    try:
        events.register_task(task)
        workspace = Workspace(fixture_root(task), events.directory / "workspace", task)
        workspace.profiles.save_snapshot(events.directory, settings=config)
        editor = Editor(workspace.policy, task.editable_globs, events)
        tests = DockerTests(workspace, events.directory, timeout=config.tool_timeout_seconds)
        dispatcher = Dispatcher(workspace.policy, events, editor=editor, tests=tests, workspace=workspace)
        adapter = model if model is not None else RepairFakeModel()
        outcome = run_loop(adapter, dispatcher, max_iterations=config.max_iterations,
                           settings=config, state=tracker.state)
        workspace.export_diff(events.directory / "candidate.patch")
        tracker.finish(outcome, generation=editor.generation,
                       patch_ref=str(events.directory / "candidate.patch"))
        return events, list(outcome.observations)
    except Exception as exc:
        diagnostic = traceback.format_exc()
        if workspace is not None:
            try:
                workspace.export_diff(events.directory / "candidate.patch")
            except Exception:  # noqa: BLE001 - preserve the original fatal error after recording export diagnostics
                events.write("patch.export_failed", diagnostic=traceback.format_exc())
        patch_path = events.directory / "candidate.patch"
        tracker.finish(None, generation=editor.generation if editor is not None else 0,
                       patch_ref=str(patch_path) if patch_path.exists() else None, diagnostic=diagnostic,
                       failure_reason="environment_error" if isinstance(exc, (OSError, ToolFailure)) else "program_error")
        raise
