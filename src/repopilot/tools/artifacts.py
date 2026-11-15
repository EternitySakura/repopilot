"""Current-run capabilities for bounded public test output, not a host-path reader."""

import hashlib
import json
import stat
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict

from repopilot.contracts.action import ReadTestResultArgs
from repopilot.contracts.base import Contract
from repopilot.contracts.tool_data import FileLine, ReadTestResultData
from repopilot.repository.profiles import TARGET_TEST
from repopilot.tools.policy import ReadPolicy, ToolFailure


class ArtifactRecord(Contract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    artifact_id: str
    run_id: str
    relative_path: str
    visibility: Literal["agent", "controller", "report"]
    kind: Literal["public_test_log", "controller_artifact"]
    sha256: str
    size_bytes: int
    source_generation: int
    source_truncated: bool
    execution_id: str | None = None
    input_manifest_sha256: str | None = None
    producer_call_id: str | None = None


class ArtifactStore:
    def __init__(self, directory: Path, run_id: str, *, capture_limit: int = TARGET_TEST.log_limit_bytes):
        self.directory = directory.absolute()
        self.run_id = run_id
        self.capture_limit = capture_limit
        self._records: dict[str, ArtifactRecord] = {}

    def restore_verified(self, records: list[dict]):
        """Caller must verify the checkpoint and ledger; disk presence never grants a capability."""
        if self._records:
            raise RuntimeError("Artifact authority can only be restored into an empty store")
        for value in records:
            record = ArtifactRecord.model_validate(value)
            if record.run_id != self.run_id:
                raise ValueError("Restored artifact belongs to another run")
            data = self._bytes(record.relative_path)
            if len(data) != record.size_bytes or hashlib.sha256(data).hexdigest() != record.sha256:
                raise ValueError("Restored artifact bytes differ from the checkpoint")
            self._records[record.artifact_id] = record

    def _path(self, relative: str) -> Path:
        # Paths originate only in controller registration, never in tool arguments.
        parts = Path(relative).parts
        if Path(relative).is_absolute() or ".." in parts:
            raise ToolFailure("ARTIFACT_DENIED", "Invalid controller artifact path")
        for node in (*reversed(self.directory.parents), self.directory):
            ReadPolicy._check_node(node)
        path = self.directory
        for part in parts:
            path = path / part
            ReadPolicy._check_node(path)
        if not path.resolve().is_relative_to(self.directory.resolve()):
            raise ToolFailure("ARTIFACT_DENIED", "Artifact escapes the current run")
        if not stat.S_ISREG(path.stat().st_mode):
            raise ToolFailure("ARTIFACT_DENIED", "Artifact must be a regular file")
        return path

    def _bytes(self, relative: str) -> bytes:
        try:
            path = self._path(relative)
            with path.open("rb") as stream:
                data = stream.read(self.capture_limit + 1)
        except FileNotFoundError:
            raise ToolFailure("ARTIFACT_NOT_FOUND", "Registered output file is unavailable") from None
        if len(data) > self.capture_limit:
            raise ToolFailure("OUTPUT_LIMIT", "Artifact exceeds the captured output limit")
        return data

    def _register(self, relative: str, *, visibility: str, kind: str, generation: int,
                  source_truncated: bool, execution_id: str | None = None,
                  input_manifest_sha256: str | None = None, producer_call_id: str | None = None) -> ArtifactRecord:
        data = self._bytes(relative)
        record = ArtifactRecord(artifact_id=uuid4().hex, run_id=self.run_id, relative_path=relative,
                                visibility=visibility, kind=kind, sha256=hashlib.sha256(data).hexdigest(),
                                size_bytes=len(data), source_generation=generation, source_truncated=source_truncated,
                                execution_id=execution_id, input_manifest_sha256=input_manifest_sha256,
                                producer_call_id=producer_call_id)
        self._records[record.artifact_id] = record
        # Disk metadata alone never grants authority; explicit restore also requires a verified ledger checkpoint.
        payload = {"run_id": self.run_id, "records": [item.model_dump() for item in self._records.values()]}
        (self.directory / "artifacts.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return record

    def register_test_log(self, call_id: UUID, *, log_ref: str, generation: int,
                          source_truncated: bool, execution_id: str | None = None,
                          input_manifest_sha256: str | None = None) -> ArtifactRecord:
        relative = (Path("tests") / str(call_id) / "output.log").as_posix()
        expected = self.directory / "tests" / str(call_id) / "output.log"
        if Path(log_ref).absolute() != expected:
            raise RuntimeError("Public test output path does not belong to this run/call")
        try:
            return self._register(relative, visibility="agent", kind="public_test_log", generation=generation,
                                  source_truncated=source_truncated, execution_id=execution_id,
                                  input_manifest_sha256=input_manifest_sha256, producer_call_id=str(call_id))
        except ToolFailure as exc:
            raise RuntimeError(f"Cannot publish completed public test output: {exc.code}") from exc

    def register_controller_file(self, relative: str) -> ArtifactRecord:
        return self._register(relative, visibility="controller", kind="controller_artifact",
                              generation=0, source_truncated=False)

    def read_test_result(self, args: ReadTestResultArgs, *, generation: int) -> ReadTestResultData:
        record = self._records.get(args.artifact_id)
        if record is None or record.run_id != self.run_id:
            raise ToolFailure("ARTIFACT_NOT_FOUND", "No such artifact in the current run")
        if record.visibility != "agent" or record.kind != "public_test_log":
            raise ToolFailure("ARTIFACT_DENIED", "Artifact is not public test output")
        data = self._bytes(record.relative_path)
        if len(data) != record.size_bytes or hashlib.sha256(data).hexdigest() != record.sha256:
            raise ToolFailure("ARTIFACT_CHANGED", "Registered test output changed", suggested_action="run_test")
        text_lines = data.decode("utf-8", errors="replace").splitlines()
        lines = []
        used = 0
        next_line = None
        for number in range(args.start_line, min(args.end_line, len(text_lines)) + 1):
            text = text_lines[number - 1]
            size = len(text.encode("utf-8")) + 1
            if used + size > args.max_bytes:
                if not lines:
                    raise ToolFailure("OUTPUT_LIMIT", "A single output line exceeds the requested byte limit")
                next_line = number
                break
            lines.append(FileLine(line=number, text=text))
            used += size
        if next_line is None and args.end_line < len(text_lines):
            next_line = args.end_line + 1
        return ReadTestResultData(
            artifact_id=record.artifact_id, lines=lines, sha256=record.sha256,
            execution_id=record.execution_id, input_manifest_sha256=record.input_manifest_sha256,
            source_generation=record.source_generation, stale=record.source_generation != generation,
            source_truncated=record.source_truncated, next_line=next_line,
            truncated=record.source_truncated or next_line is not None,
        )
