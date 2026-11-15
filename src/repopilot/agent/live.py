"""Explicitly opted-in real-model run on the fixed public fixture, followed by scoped Gate A."""

import sys
import traceback
from threading import Event

from repopilot.agent.deepseek import DeepSeekModel
from repopilot.agent.demo import PROJECT_ROOT
from repopilot.agent.experiment_condition import (
    ExperimentCondition,
    retrieve_public_leads,
)
from repopilot.agent.loop import run_loop
from repopilot.contracts.evaluation import EvaluationContext
from repopilot.contracts.task import TaskSpec
from repopilot.dispatcher import Dispatcher
from repopilot.ledger.store import configured_ledger
from repopilot.observability.events import EventLog
from repopilot.observability.reports import RunTracker
from repopilot.repository.profiles import resolve_profiles
from repopilot.repository.tasks import (
    DEFAULT_TASK_ID,
    fixture_root,
    load_development_task,
)
from repopilot.settings import LLMSettings, Settings
from repopilot.tools.edit import Editor
from repopilot.tools.errors import ToolFailure
from repopilot.tools.testing import DockerTests
from repopilot.verification.gate_a import verify_candidate
from repopilot.workspace import Workspace


def check_environment(profiles, config):
    profiles.budget.check_settings(config)
    image = DockerTests.command(["image", "inspect", profiles.repo.image_id, "--format", "{{.Id}}"])
    if image.returncode or image.stdout.decode().strip() != profiles.repo.image_id:
        raise ToolFailure("DOCKER_UNAVAILABLE", "Docker/frozen fixture image unavailable; no model request was sent")


def run_live(*, settings: Settings | None = None, llm_settings: LLMSettings | None = None,
             runs=None, verifications=None, transport=None, task_id: str = DEFAULT_TASK_ID,
             evaluation_context: EvaluationContext | None = None, cancel_event: Event | None = None,
             pause_after_tools: int | None = None, condition: ExperimentCondition | None = None):
    if condition is not None and pause_after_tools is not None:
        raise ValueError("Comparison runs cannot be paused")
    if pause_after_tools is not None and configured_ledger() is None:
        raise ValueError("Durable pause requires a configured controller ledger")
    task = load_development_task(task_id)
    return _run_task(task, settings=settings, llm_settings=llm_settings, runs=runs, verifications=verifications,
                     transport=transport, evaluation_context=evaluation_context, cancel_event=cancel_event,
                     pause_after_tools=pause_after_tools, condition=condition)


def _run_task(task: TaskSpec, *, settings=None, llm_settings=None, runs=None, verifications=None, transport=None,
              evaluation_context=None, cancel_event=None, pause_after_tools=None, condition=None):
    if evaluation_context is not None and evaluation_context.task_id != task.task_id:
        raise ValueError("Evaluation context belongs to another task")
    config = settings if settings is not None else Settings(max_iterations=8)
    llm = llm_settings if llm_settings is not None else LLMSettings()
    events = EventLog(runs if runs is not None else PROJECT_ROOT / "artifacts" / "runs")
    events.register_task(task)
    adapter = DeepSeekModel(llm, task.agent_view(), events, transport=transport)
    tracker = RunTracker(events, config, mode="live_candidate", task_id=task.task_id, accounting=adapter.accounting())
    workspace = None
    editor = None
    try:
        if evaluation_context is not None:
            events.bind_evaluation(evaluation_context)
        # Establish an executable environment before sending any paid request.
        profiles = resolve_profiles(task)
        check_environment(profiles, config)
        workspace = Workspace(fixture_root(task), events.directory / "workspace", task)
        (events.directory / "task.json").write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")
        workspace.profiles.save_snapshot(events.directory, settings=config)
        editor = Editor(workspace.policy, task.editable_globs, events)
        runner = DockerTests(workspace, events.directory, timeout=config.tool_timeout_seconds)
        dispatcher = Dispatcher(workspace.policy, events, editor=editor, tests=runner, workspace=workspace)
        adapter.configure_tools(dispatcher.enabled_tools)
        adapter.configure_context(workspace.profiles.repo.public_files, config)
        if condition is not None:
            leads = (retrieve_public_leads(workspace.policy, task.description, task.editable_globs)
                     if condition.proactive_retrieval else {})
            adapter.bind_condition(condition, leads)
    except Exception:
        _fail_live(events, tracker, adapter, workspace, editor)
        raise
    return _drive_live(adapter, events, tracker, workspace, editor, dispatcher, config,
                       verifications=verifications, cancel_event=cancel_event, pause_after_tools=pause_after_tools)


def _fail_live(events, tracker, adapter, workspace, editor):
    exc = sys.exc_info()[1]
    diagnostic = traceback.format_exc()
    if workspace is not None:
        try:
            workspace.export_diff(events.directory / "candidate.patch")
        except Exception:  # noqa: BLE001 - preserve primary failure
            events.write("patch.export_failed", diagnostic=traceback.format_exc())
    tracker.accounting = adapter.accounting()
    patch = events.directory / "candidate.patch"
    tracker.finish(None, generation=editor.generation if editor is not None else 0,
                   patch_ref=str(patch) if patch.exists() else None, diagnostic=diagnostic,
                   failure_reason="environment_error" if isinstance(exc, (ToolFailure, OSError)) else "program_error")


def _drive_live(adapter, events, tracker, workspace, editor, dispatcher, config, *,
                verifications=None, cancel_event=None, pause_after_tools=None):
    try:
        outcome = run_loop(adapter, dispatcher, max_iterations=config.max_iterations,
                           settings=config, state=tracker.state, cancel_event=cancel_event, pause_after_tools=pause_after_tools)
        workspace.export_diff(events.directory / "candidate.patch")
        tracker.accounting = adapter.accounting()
        tracker.finish(outcome, generation=editor.generation, patch_ref=str(events.directory / "candidate.patch"))
    except Exception:
        _fail_live(events, tracker, adapter, workspace, editor)
        raise
    if outcome.status == "paused":
        from repopilot.agent.checkpoints import save_checkpoint
        try:
            save_checkpoint(events, tracker, adapter, editor)
        except Exception:
            events.write("run.checkpoint_failed", diagnostic=traceback.format_exc())
            raise
    # Independent verification is outside the completed model loop: errors never resume the model.
    verification = None
    if (outcome.status == "script_finished" and editor.generation
            and (cancel_event is None or not cancel_event.is_set())):
        options = {"evaluation_context": events.evaluation_context} if events.evaluation_context is not None else {}
        verification = verify_candidate(events.directory, verifications, settings=config, task_id=workspace.task_id, **options)
        events.write("verification.linked", verification_id=verification.verification_id,
                     status=verification.status, scope="public_gate_a", benchmark_success=False)
    return events, list(outcome.observations), verification
