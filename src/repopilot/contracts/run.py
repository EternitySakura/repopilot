"""Day 5 terminal states deliberately contain no independent success verdict."""

from enum import Enum
from typing import Literal

from pydantic import Field

from repopilot.contracts.base import Contract
from repopilot.contracts.evaluation import EvaluationContext


class RunStatus(str, Enum):
    SCRIPT_FINISHED = "script_finished"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_PROGRESS = "no_progress"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class RunAccounting(Contract):
    usage_status: Literal["not_applicable"] = "not_applicable"
    cost_status: Literal["not_applicable"] = "not_applicable"
    tokens: None = None
    cost_usd: None = None


class LLMAccounting(Contract):
    usage_status: Literal["actual", "unknown"]
    cost_status: Literal["upper_bound"] = "upper_bound"
    tokens: int | None = Field(ge=0)
    cost_usd: None = None
    provider: Literal["deepseek"] = "deepseek"
    requested_model: str
    response_models: list[str]
    requests: int = Field(ge=0)
    unknown_usage_calls: int = Field(ge=0)
    known_prompt_tokens: int = Field(ge=0)
    known_completion_tokens: int = Field(ge=0)
    known_cache_hit_tokens: int = Field(ge=0)
    cost_upper_bound_usd: str
    max_cost_usd: str
    rate_snapshot: dict[str, str]


class RunCounters(Contract):
    decisions: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    edit_calls: int = Field(ge=0)
    test_calls: int = Field(ge=0)
    failed_tool_calls: int = Field(ge=0)
    no_progress_streak: int = Field(ge=0)


class RunLimits(Contract):
    max_iterations: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)
    max_edit_calls: int = Field(ge=1)
    max_test_calls: int = Field(ge=1)
    max_no_progress: int = Field(ge=1)
    max_run_seconds: int | None = Field(default=None, ge=1, le=3600)


class ModelFailure(Contract):
    tool_call_id: str
    code: str
    duration_ms: int | None = Field(default=None, ge=0)
    # Content-free, flat metadata built by the controller. No response excerpts.
    response_diagnostic: dict[str, str | int | bool | None] | None = None


class RunReport(Contract):
    run_id: str
    mode: Literal["scripted_readonly", "scripted_candidate", "live_candidate", "reconstructed_candidate",
                  "imported_candidate", "holdout_reference"]
    task_id: str | None
    status: RunStatus
    stop_reason: str
    started_at: str
    finished_at: str
    duration_ms: int = Field(ge=0)
    counters: RunCounters
    limits: RunLimits
    accounting: RunAccounting | LLMAccounting = Field(default_factory=RunAccounting)
    workspace_generation: int = Field(ge=0)
    candidate_assessment: Literal[
        "not_tested", "public_tests_passed", "public_tests_failed", "stale_test_results",
    ]
    public_tests_passed: bool
    last_test_execution_id: str | None = None
    tested_input_manifest_sha256: str | None = None
    independently_verified: Literal[False] = False
    bug_fixed: Literal[False] = False
    failure_fingerprints: dict[str, int]
    artifacts: dict[str, str]
    diagnostic: str | None = None
    model_failure: ModelFailure | None = None
    evaluation_context: EvaluationContext | None = None
