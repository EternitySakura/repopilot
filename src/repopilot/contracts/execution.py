"""Controller-owned test execution identity and terminal evidence."""

from typing import Literal

from pydantic import ConfigDict, Field

from repopilot.contracts.base import Contract

ExecutionState = Literal["preparing", "running", "completed", "failed", "timed_out", "unknown"]
CleanupState = Literal["not_needed", "pending", "confirmed", "unknown"]
FINAL_VERIFICATION_MODE = "final_private_gate_b"
EXECUTION_ACTIONS = {"public_tests": "run_test", "public_gate_a": "gate_a_public_tests",
                     "private_gate_b": "gate_b_private_tests"}


class ExecutionRecord(Contract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    run_id: str
    tool_call_id: str
    scope: Literal["public_tests", "public_gate_a", "private_gate_b"]
    source_generation: int = Field(ge=0)
    input_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hashes: dict[str, str]
    state: ExecutionState = "preparing"
    phase: str = "prepared"
    cleanup_status: CleanupState = "not_needed"
    started_at: str
    finished_at: str | None = None
    context_sha256: str | None = None
    resources: dict[str, str] = Field(default_factory=dict)
    exit_code: int | None = None
    passed: int | None = None
    failed: int | None = None
    public_tests_passed: bool = False
    artifact_id: str | None = None
    error_code: str | None = None
    diagnostic: str | None = None


class ExecutionStatusData(Contract):
    execution_id: str
    tool_call_id: str
    state: ExecutionState
    cleanup_status: CleanupState
    source_generation: int
    input_manifest_sha256: str
    exit_code: int | None
    passed: int | None
    failed: int | None
    public_tests_passed: bool
    artifact_id: str | None
    error_code: str | None
    stale: bool
    truncated: bool = False
