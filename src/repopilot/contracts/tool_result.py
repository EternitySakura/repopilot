from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from repopilot.contracts.base import Contract


class ToolError(Contract):
    code: str = Field(min_length=1)
    message: str
    retryable: bool = False
    suggested_action: str | None = None


class ToolResult(Contract):
    tool_call_id: UUID
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None
    artifact_ref: str | None = None
    truncated: bool = False
    workspace_generation: int = Field(ge=0)
    duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def consistent_status(self) -> "ToolResult":
        if self.ok != (self.error is None):
            raise ValueError("ok must agree with error presence")
        return self
