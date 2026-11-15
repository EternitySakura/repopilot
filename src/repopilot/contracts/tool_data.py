"""Successful tool payloads, serialized into ToolResult.data.

Executors also copy payload truncation into ToolResult.truncated.
"""

from typing import Literal

from pydantic import Field

from repopilot.contracts.base import Contract


class DirectoryEntry(Contract):
    path: str = Field(min_length=1)
    type: Literal["file", "directory"]


class ListDirectoryData(Contract):
    entries: list[DirectoryEntry]
    truncated: bool = False


class SearchMatch(Contract):
    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    text: str


class SearchCodeData(Contract):
    matches: list[SearchMatch]
    truncated: bool = False


class SymbolMatch(Contract):
    path: str
    name: str
    qualified_name: str
    kind: Literal["function", "class", "method"]
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str
    is_async: bool = False
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SymbolFileIssue(Contract):
    path: str
    code: Literal["SYNTAX_ERROR", "FILE_TRUNCATED"]
    line: int | None = Field(default=None, ge=0)


class SearchSymbolData(Contract):
    symbols: list[SymbolMatch]
    skipped_files: list[SymbolFileIssue]
    files_scanned: int = Field(ge=0)
    bytes_read: int = Field(ge=0)
    truncated: bool = False


class FileLine(Contract):
    line: int = Field(ge=1)
    text: str


class ReadFileData(Contract):
    lines: list[FileLine]
    # A byte-truncated read cannot claim a hash of the complete file.
    sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    truncated: bool = False


class EditFileData(Contract):
    path: str
    before_sha256: str | None
    after_sha256: str
    truncated: bool = False


class EditFilesData(Contract):
    files: list[EditFileData]
    changed_files: int = Field(ge=0, le=5)
    before_sha256: str
    after_sha256: str
    transaction_id: str | None = None
    truncated: bool = False


class RunTestData(Contract):
    exit_code: int
    passed: int
    failed: int
    public_tests_passed: bool
    log_ref: str
    image_id: str
    artifact_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    execution_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    input_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_generation: int | None = Field(default=None, ge=0)
    cleanup_status: Literal["confirmed", "not_needed"] | None = None
    stale: bool = False
    truncated: bool = False


class ReadTestResultData(Contract):
    artifact_id: str
    execution_id: str | None = None
    input_manifest_sha256: str | None = None
    lines: list[FileLine]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_generation: int = Field(ge=0)
    stale: bool
    source_truncated: bool
    next_line: int | None = Field(default=None, ge=1)
    truncated: bool = False


class DiffFile(Contract):
    path: str
    status: Literal["added", "modified"]
    before_sha256: str | None
    after_sha256: str


class GitDiffData(Contract):
    base_revision: str
    files: list[DiffFile]
    diff: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    offset: int = Field(ge=0)
    next_offset: int | None = Field(default=None, ge=0)
    truncated: bool = False
