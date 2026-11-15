"""Controller-owned clean-boundary checkpoint. API secrets are not fields."""

from typing import Any, Literal

from pydantic import Field

from repopilot.contracts.base import Contract
from repopilot.contracts.candidate import CandidateManifest
from repopilot.contracts.run import LLMAccounting, RunCounters
from repopilot.contracts.tool_result import ToolResult


class LoopCheckpoint(Contract):
    counters: RunCounters
    observations: list[ToolResult]
    evidence: list[str]
    failure_fingerprints: dict[str, int]
    last_test: ToolResult | None


class Checkpoint(Contract):
    version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    task_id: str
    task_sha256: str
    profile_sha256: str
    code_sha256: str
    prompt_version: str
    parser_version: str
    schema_sha256: str
    enabled_tools: list[str]
    settings: dict[str, Any]
    llm: dict[str, Any]
    accounting: LLMAccounting
    assistant_decisions: list[dict]
    loop: LoopCheckpoint
    candidate: CandidateManifest
    generation: int = Field(ge=0)
    events_count: int = Field(ge=1)
    events_sha256: str
    seals: dict[str, str]


class CheckpointEnvelope(Contract):
    checkpoint_sha256: str
    checkpoint: Checkpoint
