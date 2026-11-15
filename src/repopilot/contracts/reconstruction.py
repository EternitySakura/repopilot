"""A fixed source revision for rebuilding a new candidate, never execution state."""

from typing import Literal

from pydantic import Field

from repopilot.contracts.base import Contract
from repopilot.contracts.candidate import CandidateManifest
from repopilot.contracts.evaluation import SHA256


class ReconstructionPlan(Contract):
    version: Literal[1] = 1
    source_run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source_events_count: int = Field(ge=1)
    source_seals: dict[str, str]
    source_status: str
    source_unresolved_calls: list[str]
    task_id: str
    task_sha256: str = Field(pattern=SHA256)
    profile_sha256: str = Field(pattern=SHA256)
    selected_call_id: str
    selected_batch_status: Literal["prepared", "committed", "rolled_back", "rollback_failed"]
    revision: Literal["before", "proposed"]
    candidate: CandidateManifest


class ReconstructionEnvelope(Contract):
    plan_sha256: str = Field(pattern=SHA256)
    plan: ReconstructionPlan
