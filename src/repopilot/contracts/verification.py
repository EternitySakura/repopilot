from typing import Literal

from pydantic import Field

from repopilot.contracts.base import Contract
from repopilot.contracts.evaluation import EvaluationContext
from repopilot.repository.profiles import TARGET_TEST

EXPECTED_TESTS = TARGET_TEST.expected_nodes


class TestPhase(Contract):
    nodeid: str
    when: Literal["setup", "call", "teardown"]
    outcome: Literal["passed", "failed", "skipped"]


class TestRecord(Contract):
    exit_code: int
    collected: list[str] = Field(max_length=100)
    reports: list[TestPhase] = Field(max_length=300)


class VerificationScope(Contract):
    gate: Literal["public_gate_a"] = "public_gate_a"
    repo_profile: str = Field(default="fixture_text", min_length=1)
    expected_tests: list[str] = Field(default_factory=lambda: list(EXPECTED_TESTS))
    hidden_tests: Literal[False] = False
    additional_regression_tests: Literal[False] = False
    final_gate_b: Literal[False] = False


class VerificationReport(Contract):
    verification_id: str
    task_id: str | None = None
    evaluation_context: EvaluationContext | None = None
    source_run_id: str
    verifier_version: Literal["fixture-gate-a-v1"] = "fixture-gate-a-v1"
    status: Literal["passed", "failed_checks", "error", "timed_out", "cancelled"]
    verification_scope: VerificationScope = Field(default_factory=VerificationScope)
    failed_checks: list[str]
    cleanroom_verified: bool
    benchmark_success: Literal[False] = False
    base_revision: str | None
    profile_sha256: str | None = None
    patch_sha256: str | None
    manifest_sha256: str | None
    environment_digest: str
    candidate_image_id: str | None
    execution_id: str | None = None
    input_manifest_sha256: str | None = None
    source_hashes: dict[str, str]
    original_test_hashes: dict[str, str]
    test_record: TestRecord | None
    test_attempts: int = Field(ge=0, le=1)
    artifacts: dict[str, str]
    message: str
    diagnostic: str | None = None
