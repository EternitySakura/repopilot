import traceback
from pathlib import Path

from repopilot.agent.loop import run_loop
from repopilot.agent.model_adapter import ModelAdapter, demo_model
from repopilot.dispatcher import Dispatcher
from repopilot.observability.events import EventLog
from repopilot.observability.reports import RunTracker
from repopilot.repository.profiles import FIXTURE_REPO, resolve_profiles
from repopilot.repository.tasks import (
    DEFAULT_TASK_ID,
    fixture_root,
    load_development_task,
)
from repopilot.settings import Settings
from repopilot.tools.policy import ReadPolicy, ToolFailure

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = PROJECT_ROOT / FIXTURE_REPO.fixture_directory


def run_demo(runs: Path | None = None, model: ModelAdapter | None = None, *,
             settings: Settings | None = None, scenario: str = "normal", task_id: str = DEFAULT_TASK_ID):
    task = load_development_task(task_id)
    config = settings if settings is not None else Settings()
    events = EventLog(runs if runs is not None else PROJECT_ROOT / "artifacts" / "runs")
    tracker = RunTracker(events, config, mode="scripted_readonly", task_id=task_id)
    try:
        events.register_task(task)
        profiles = resolve_profiles(task)
        profiles.save_snapshot(events.directory, settings=config)
        dispatcher = Dispatcher(ReadPolicy(fixture_root(task), profile=profiles.repo), events)
        adapter = model if model is not None else demo_model(scenario)
        outcome = run_loop(adapter, dispatcher, max_iterations=config.max_iterations,
                           settings=config, state=tracker.state)
        tracker.finish(outcome)
    except Exception as exc:
        tracker.finish(None, diagnostic=traceback.format_exc(),
                       failure_reason="environment_error" if isinstance(exc, (OSError, ToolFailure)) else "program_error")
        raise
    return events, list(outcome.observations)
