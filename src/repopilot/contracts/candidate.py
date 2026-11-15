from typing import Literal

from pydantic import Field

from repopilot.contracts.action import MAX_CHANGE_FILES, RepositoryPathArgs
from repopilot.contracts.base import Contract

MAX_CANDIDATE_FILES = MAX_CHANGE_FILES

class CandidateChange(RepositoryPathArgs):
    before_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Exact UTF-8 bytes are reconstructed without importing or executing target code.
    content: str = Field(max_length=65536)


class CandidateManifest(Contract):
    version: Literal[1] = 1
    task_id: str | None = Field(default=None, min_length=1)
    base_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes: list[CandidateChange] = Field(max_length=MAX_CANDIDATE_FILES)
