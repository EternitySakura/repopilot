"""Controller-owned budgets and evidence-based stopping, without repair verdicts."""

import hashlib
import json
from dataclasses import dataclass, field
from threading import Event
from time import monotonic

from repopilot.agent.errors import ModelBudgetExceeded, ModelProviderError
from repopilot.agent.model_adapter import ModelAdapter
from repopilot.agent.runtime import deadline_scope
from repopilot.contracts.action import EDIT_ACTIONS, Action, action_name
from repopilot.contracts.run import RunStatus
from repopilot.contracts.tool_result import ToolResult
from repopilot.dispatcher import Dispatcher
from repopilot.settings import Settings


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class LoopState:
    started: float = field(default_factory=monotonic)
    decisions: int = 0
    tool_calls: int = 0
    edit_calls: int = 0
    test_calls: int = 0
    failed_tool_calls: int = 0
    no_progress_streak: int = 0
    observations: list[ToolResult] = field(default_factory=list)
    evidence: set[str] = field(default_factory=set)
    failure_fingerprints: dict[str, int] = field(default_factory=dict)
    last_test: ToolResult | None = None

    def observe(self, payload, result: ToolResult) -> None:
        self.observations.append(result)
        kind = action_name(payload)
        failed = not result.ok or (kind == "run_test" and not result.data.get("public_tests_passed"))
        if not result.ok:
            self.failed_tool_calls += 1
        if kind == "run_test":
            self.last_test = result
        # Artifacts, timings and random image/call IDs are not evidence of progress.
        stable_data = {key: value for key, value in result.data.items()
                       if key not in {"log_ref", "image_id", "artifact_id", "execution_id", "tool_call_id", "transaction_id"}}
        identity = {"type": kind, "result": stable_data, "generation": result.workspace_generation}
        if isinstance(payload, dict) and isinstance(payload.get("arguments"), dict):
            identity["path"] = payload["arguments"].get("path")
            if kind in {"search_code", "search_symbol"}:
                identity["query"] = payload["arguments"].get("query")
            if kind == "search_symbol":
                identity["symbol_kind"] = payload["arguments"].get("kind", "any")
        if failed:
            failure = fingerprint({"action": payload, "generation": result.workspace_generation,
                                   "error_code": result.error.code if result.error else "PUBLIC_TEST_FAILED",
                                   "result": stable_data})
            self.failure_fingerprints[failure] = self.failure_fingerprints.get(failure, 0) + 1
        key = fingerprint(identity)
        # A failed public test gives new evidence once; rejected actions never make progress.
        progress = result.ok and key not in self.evidence
        if kind in EDIT_ACTIONS and result.ok:
            progress = result.data["before_sha256"] != result.data["after_sha256"]
        self.no_progress_streak = 0 if progress else self.no_progress_streak + 1
        self.evidence.add(key)


@dataclass(frozen=True)
class LoopOutcome:
    status: RunStatus
    decisions: int
    observations: tuple[ToolResult, ...]
    stop_reason: str


def run_loop(model: ModelAdapter, dispatcher: Dispatcher, *, max_iterations: int,
             settings: Settings | None = None, state: LoopState | None = None,
             cancel_event: Event | None = None, pause_after_tools: int | None = None) -> LoopOutcome:
    """Each decide call (including None and invalid actions) consumes one iteration."""
    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer")
    if pause_after_tools is not None and (type(pause_after_tools) is not int or pause_after_tools < 1):
        raise ValueError("pause_after_tools must be a positive integer")
    config = settings if settings is not None else Settings()
    current = state if state is not None else LoopState()
    deadline = current.started + config.max_run_seconds if config.max_run_seconds is not None else None
    with deadline_scope(deadline):
        try:
            return _run_loop(model, dispatcher, max_iterations=max_iterations, config=config,
                             current=current, cancel_event=cancel_event, pause_after_tools=pause_after_tools)
        except KeyboardInterrupt:
            dispatcher.events.write("run.cancellation_observed", source="keyboard_interrupt")
            return LoopOutcome(RunStatus.CANCELLED, current.decisions, tuple(current.observations), "user_cancelled")


def _run_loop(model: ModelAdapter, dispatcher: Dispatcher, *, max_iterations: int,
              config: Settings, current: LoopState, cancel_event: Event | None, pause_after_tools: int | None) -> LoopOutcome:

    def stop(status: RunStatus, reason: str) -> LoopOutcome:
        if status == RunStatus.CANCELLED:
            dispatcher.events.write("run.cancellation_observed", source="controller_event")
        return LoopOutcome(status, current.decisions, tuple(current.observations), reason)

    def expired() -> bool:
        return config.max_run_seconds is not None and monotonic() - current.started >= config.max_run_seconds

    while current.decisions < max_iterations:
        if cancel_event is not None and cancel_event.is_set():
            return stop(RunStatus.CANCELLED, "user_cancelled")
        if expired():
            return stop(RunStatus.TIMED_OUT, "run_time_limit")
        if current.tool_calls >= config.max_tool_calls:
            return stop(RunStatus.BUDGET_EXHAUSTED, "tool_call_limit")
        if pause_after_tools is not None and current.tool_calls >= pause_after_tools:
            return stop(RunStatus.PAUSED, "controller_pause")
        # The adapter sees actual feedback but cannot mutate the controller's history.
        snapshot = tuple(result.model_copy(deep=True) for result in current.observations)
        current.decisions += 1
        try:
            action = model.decide(snapshot)
        except ModelBudgetExceeded as exc:
            if expired():
                return stop(RunStatus.TIMED_OUT, "run_time_limit")
            return stop(RunStatus.BUDGET_EXHAUSTED, str(exc))
        except ModelProviderError as exc:
            if expired():
                return stop(RunStatus.TIMED_OUT, "run_time_limit")
            return stop(RunStatus.FAILED, f"model_provider_error:{exc}")
        if cancel_event is not None and cancel_event.is_set():
            return stop(RunStatus.CANCELLED, "user_cancelled")
        if expired():
            return stop(RunStatus.TIMED_OUT, "run_time_limit")
        if action is None:
            return stop(RunStatus.SCRIPT_FINISHED, "script_finished")
        payload = action.model_dump(mode="json") if isinstance(action, Action) else action
        kind = action_name(payload)
        if kind in EDIT_ACTIONS and current.edit_calls >= config.max_edit_calls:
            return stop(RunStatus.BUDGET_EXHAUSTED, "edit_call_limit")
        if kind == "run_test" and current.test_calls >= config.max_test_calls:
            return stop(RunStatus.BUDGET_EXHAUSTED, "test_call_limit")
        # Count attempts before dispatch, including invalid arguments and fatal executions.
        current.tool_calls += 1
        current.edit_calls += int(kind in EDIT_ACTIONS)
        current.test_calls += int(kind == "run_test")
        try:
            result = dispatcher.dispatch(action)
        except KeyboardInterrupt:
            current.failed_tool_calls += 1
            if kind in EDIT_ACTIONS or kind == "run_test":
                current.last_test = None
            raise
        except Exception:
            current.failed_tool_calls += 1
            raise
        current.observe(payload, result)
        if cancel_event is not None and cancel_event.is_set():
            return stop(RunStatus.CANCELLED, "user_cancelled")
        if expired():
            return stop(RunStatus.TIMED_OUT, "run_time_limit")
        if result.error and result.error.code == "TEST_TIMEOUT":
            return stop(RunStatus.TIMED_OUT, "test_timeout")
        if result.error and result.error.code in {"DOCKER_UNAVAILABLE", "TEST_SETUP_FAILED"}:
            return stop(RunStatus.FAILED, "environment_error")
        if kind == "run_test" and result.ok:
            if result.data.get("stale"):
                return stop(RunStatus.FAILED, "test_input_changed")
            if result.data.get("exit_code") not in {0, 1}:
                return stop(RunStatus.FAILED, "test_environment_error")
            if result.truncated or (result.data.get("exit_code") == 0
                                    and not result.data.get("public_tests_passed")):
                return stop(RunStatus.FAILED, "test_result_invalid")
        if current.no_progress_streak >= config.max_no_progress:
            return stop(RunStatus.NO_PROGRESS, "no_progress_limit")
    return stop(RunStatus.BUDGET_EXHAUSTED, "iteration_limit")
