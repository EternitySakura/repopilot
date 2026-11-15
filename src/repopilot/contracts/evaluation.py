"""Frozen development evaluation conditions; credentials are never plan fields."""

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from repopilot.contracts.base import Contract

SHA256 = r"^[0-9a-f]{64}$"
ConditionName = Literal["B0", "B1", "B1-no-retrieval", "B1-recent-context"]


class EvaluationContext(Contract):
    evaluation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=SHA256)
    task_id: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1, le=3)
    condition: ConditionName | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def single_development_attempt(self):
        if self.condition is None and self.attempt != 1:
            raise ValueError("Development evaluations allow one attempt")
        return self


class EvaluationModel(Contract):
    model_id: Literal["deepseek-flash"]
    base_url: str
    max_calls: int = Field(ge=1, le=8)
    max_output_tokens: int = Field(ge=128, le=4096)
    max_input_tokens: int = Field(ge=4096, le=48000)
    max_total_tokens: int = Field(ge=4096, le=200000)
    max_cost_usd: Decimal = Field(gt=0, le=Decimal("0.05"))
    timeout_seconds: int = Field(ge=1, le=120)


class EvaluationLimits(Contract):
    max_iterations: int = Field(ge=1, le=8)
    max_tool_calls: int = Field(ge=1, le=8)
    max_edit_calls: int = Field(ge=1, le=2)
    max_test_calls: int = Field(ge=1, le=2)
    max_no_progress: int = Field(ge=1, le=3)
    tool_timeout_seconds: int = Field(ge=1, le=60)
    # Absent in historical plans; None retains their original unbounded loop.
    max_run_seconds: int | None = Field(default=None, ge=1, le=3600)


class EvaluationTask(Contract):
    task_id: str
    task_sha256: str = Field(pattern=SHA256)
    profile_sha256: str = Field(pattern=SHA256)
    fixture_hashes: dict[str, str]


class EvaluationPlan(Contract):
    version: Literal[1] = 1
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    mode: Literal["offline_supplier", "live"] = "offline_supplier"
    attempts_per_task: Literal[1] = 1
    tasks: list[EvaluationTask] = Field(min_length=1, max_length=100)
    model: EvaluationModel
    limits: EvaluationLimits
    code_sha256: str = Field(pattern=SHA256)
    prompt_version: str
    parser_version: str
    diagnostic_policy: str
    enabled_tools: list[str]
    schema_sha256: str = Field(pattern=SHA256)
    rate_snapshot: dict[str, str]
    request_options: dict[str, str | int | bool]

    @model_validator(mode="after")
    def unique_tasks(self):
        identities = [task.task_id for task in self.tasks]
        if len(identities) != len(set(identities)):
            raise ValueError("Evaluation tasks must be distinct")
        return self


class EvaluationEnvelope(Contract):
    plan_sha256: str = Field(pattern=SHA256)
    plan: EvaluationPlan
