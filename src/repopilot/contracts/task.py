from pathlib import PurePosixPath

from pydantic import Field, field_validator

from repopilot.contracts.base import Contract


class AgentTaskView(Contract):
    task_id: str = Field(min_length=1)
    repo_profile: str = Field(min_length=1)
    base_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str = Field(min_length=1)
    editable_globs: list[str] = Field(min_length=1)
    public_test_profile: str = Field(min_length=1)
    budget_profile: str = Field(min_length=1)

    @field_validator("editable_globs")
    @classmethod
    def relative_source_paths(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            path = PurePosixPath(pattern)
            if (
                not pattern.startswith("src/")
                or ".." in path.parts
                or "\\" in pattern
                or ":" in pattern
                or ".git" in path.parts
            ):
                raise ValueError("editable globs must stay under src/ using POSIX paths")
        return patterns


class TaskSpec(AgentTaskView):
    verifier_profile: str = Field(min_length=1)

    def agent_view(self) -> AgentTaskView:
        return AgentTaskView.model_validate(
            self.model_dump(include=set(AgentTaskView.model_fields))
        )
