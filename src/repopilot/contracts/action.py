from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import Field, RootModel, field_validator, model_validator

from repopilot.contracts.base import Contract
from repopilot.repository.profiles import FIXTURE_REPO

MAX_CHANGE_FILES = 5
EDIT_ACTIONS = frozenset({"edit_file", "apply_patch", "edit_files"})


class RepositoryPathArgs(Contract):
    path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if (
            PurePosixPath(value).is_absolute()
            or PureWindowsPath(value).drive
            or "\\" in value
            or ".." in PurePosixPath(value).parts
            or ":" in value
            or "\x00" in value
        ):
            raise ValueError("path must be a relative POSIX repository path")
        return value


class ListDirectoryArgs(RepositoryPathArgs):
    path: str = Field(default=".", min_length=1)
    depth: int = Field(default=1, ge=1, le=FIXTURE_REPO.max_directory_depth)
    limit: int = Field(default=100, ge=1, le=FIXTURE_REPO.max_directory_entries)


class SearchCodeArgs(RepositoryPathArgs):
    """Search query is literal text, never a regex or semantic query."""

    path: str = Field(default=".", min_length=1)
    query: str = Field(min_length=1)
    limit: int = Field(default=100, ge=1, le=FIXTURE_REPO.max_search_matches)


class ReadFileArgs(RepositoryPathArgs):
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)

    @model_validator(mode="after")
    def ordered_lines(self) -> "ReadFileArgs":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class SearchSymbolArgs(RepositoryPathArgs):
    """Find Python definitions by exact name or lexical qualified name."""

    path: str = Field(default=".", min_length=1)
    query: str = Field(min_length=1, max_length=256)
    kind: Literal["any", "function", "class", "method"] = "any"
    limit: int = Field(default=100, ge=1, le=FIXTURE_REPO.max_search_matches)

    @field_validator("query")
    @classmethod
    def symbol_name(cls, value: str) -> str:
        if not all(part.isidentifier() for part in value.split(".")):
            raise ValueError("query must be a Python name or dot-separated qualified name")
        return value


class SearchSymbolAction(Contract):
    type: Literal["search_symbol"]
    arguments: SearchSymbolArgs


class ListDirectoryAction(Contract):
    type: Literal["list_directory"]
    arguments: ListDirectoryArgs


class SearchCodeAction(Contract):
    type: Literal["search_code"]
    arguments: SearchCodeArgs


class ReadFileAction(Contract):
    type: Literal["read_file"]
    arguments: ReadFileArgs


class EditFileArgs(RepositoryPathArgs):
    mode: Literal["replace", "create"] = "replace"
    expected_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    old_text: str = Field(max_length=65536)
    new_text: str = Field(max_length=65536)

    @model_validator(mode="after")
    def valid_edit(self) -> "EditFileArgs":
        if self.mode == "replace" and (self.expected_sha256 is None or not self.old_text):
            raise ValueError("replace requires a file hash and non-empty old_text")
        if self.mode == "create" and (self.expected_sha256 is not None or self.old_text):
            raise ValueError("create requires null expected_sha256 and empty old_text")
        return self


class EditFileAction(Contract):
    type: Literal["edit_file"]
    arguments: EditFileArgs


class EditFilesArgs(Contract):
    files: list[EditFileArgs] = Field(min_length=1, max_length=MAX_CHANGE_FILES)

    @model_validator(mode="after")
    def unique_paths(self):
        paths = [PurePosixPath(item.path).as_posix().casefold() for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("A change set must use distinct file paths")
        return self


class EditFilesAction(Contract):
    type: Literal["edit_files"]
    arguments: EditFilesArgs


class ApplyPatchArgs(RepositoryPathArgs):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    patch: str = Field(min_length=1, max_length=131072)


class ApplyPatchAction(Contract):
    type: Literal["apply_patch"]
    arguments: ApplyPatchArgs


class RunTestArgs(Contract):
    profile: Literal["target"] = "target"


class RunTestAction(Contract):
    type: Literal["run_test"]
    arguments: RunTestArgs


class ReadTestResultArgs(Contract):
    artifact_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)
    max_bytes: int = Field(default=16384, ge=1, le=65536)

    @model_validator(mode="after")
    def ordered_lines(self) -> "ReadTestResultArgs":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ReadTestResultAction(Contract):
    type: Literal["read_test_result"]
    arguments: ReadTestResultArgs


class GitDiffArgs(Contract):
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=16384, ge=1, le=65536)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def continuation_hash(self) -> "GitDiffArgs":
        if self.offset and self.expected_sha256 is None:
            raise ValueError("continuation requires expected_sha256 from the previous diff")
        return self


class GitDiffAction(Contract):
    type: Literal["git_diff"]
    arguments: GitDiffArgs


class GetExecutionStatusArgs(Contract):
    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class GetExecutionStatusAction(Contract):
    type: Literal["get_execution_status"]
    arguments: GetExecutionStatusArgs


class Action(RootModel[Annotated[
    ListDirectoryAction | SearchCodeAction | ReadFileAction | EditFileAction | RunTestAction | ReadTestResultAction | GitDiffAction | GetExecutionStatusAction | SearchSymbolAction | ApplyPatchAction | EditFilesAction,
    Field(discriminator="type"),
]]):
    """Parsed action is available as root, type and arguments."""

    @property
    def type(self) -> str:
        return self.root.type

    @property
    def arguments(self) -> ListDirectoryArgs | SearchCodeArgs | ReadFileArgs | EditFileArgs | RunTestArgs | ReadTestResultArgs | GitDiffArgs | GetExecutionStatusArgs | SearchSymbolArgs | ApplyPatchArgs | EditFilesArgs:
        return self.root.arguments


def action_name(payload) -> str | None:
    """Extract event/counter metadata; Dispatcher still validates the full payload."""
    value = payload.get("type") if isinstance(payload, dict) else None
    return value if isinstance(value, str) else None
