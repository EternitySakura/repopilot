"""Current-run execution authority; checkpoint restoration requires ledger verification."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from repopilot.contracts.execution import (
    EXECUTION_ACTIONS,
    ExecutionRecord,
    ExecutionStatusData,
)
from repopilot.observability.events import EventLog
from repopilot.tools.errors import ToolFailure

TERMINAL = {"completed", "failed", "timed_out", "unknown"}


class ExecutionStore:
    def __init__(self, directory: Path):
        self.directory = directory.absolute()
        self.run_id = directory.name
        self.events: EventLog | None = None
        self._records: dict[str, ExecutionRecord] = {}
        self._calls: set[str] = set()

    def bind(self, events: EventLog) -> None:
        if events.directory.absolute() != self.directory or events.run_id != self.run_id:
            raise ValueError("Execution store must belong to the same run")
        self.events = events

    def restore_verified(self, records: list[dict]):
        """Restore only ledger-verified terminal executions, without issuing effects."""
        if self._records:
            raise RuntimeError("Execution authority can only be restored into an empty store")
        for value in records:
            record = ExecutionRecord.model_validate(value)
            if (record.run_id != self.run_id or record.state != "completed"
                    or record.cleanup_status != "confirmed"):
                raise ValueError("Only completed executions with confirmed cleanup can resume")
            self._records[record.execution_id] = record
            self._calls.add(record.tool_call_id)

    def _save(self, record: ExecutionRecord, event: str) -> None:
        self._records[record.execution_id] = record
        payload = {"version": "execution-audit-v1", "run_id": self.run_id,
                   "records": [row.model_dump() for row in self._records.values()]}
        (self.directory / "executions.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if self.events is not None:
            self.events.write(event, tool_call_id=record.tool_call_id,
                              action_type=EXECUTION_ACTIONS[record.scope],
                              execution_id=record.execution_id, scope=record.scope, state=record.state,
                              phase=record.phase, cleanup_status=record.cleanup_status,
                              input_manifest_sha256=record.input_manifest_sha256,
                              source_generation=record.source_generation, error_code=record.error_code,
                              execution_record=record.model_dump())

    def begin(self, call_id, *, generation: int, scope: str, input_sha256: str,
              source_hashes: dict[str, str]) -> ExecutionRecord:
        if str(call_id) in self._calls:
            raise RuntimeError("A test call cannot be executed twice")
        self._calls.add(str(call_id))
        record = ExecutionRecord(execution_id=uuid4().hex, run_id=self.run_id, tool_call_id=str(call_id),
                                 scope=scope, source_generation=generation, input_manifest_sha256=input_sha256,
                                 source_hashes=source_hashes, started_at=datetime.now(UTC).isoformat())
        self._save(record, "execution.prepared")
        return record

    def update(self, execution_id: str, **changes) -> ExecutionRecord:
        before = self._records[execution_id]
        if before.state in TERMINAL:
            raise RuntimeError("Terminal execution state cannot transition or be replayed")
        record = ExecutionRecord.model_validate({**before.model_dump(), **changes})
        self._save(record, "execution.updated")
        return record

    def finish(self, execution_id: str, *, state: str, **changes) -> ExecutionRecord:
        if state not in TERMINAL:
            raise ValueError("Expected a terminal execution state")
        before = self._records[execution_id]
        if before.state in TERMINAL:
            raise RuntimeError("Execution already finished")
        if state != "unknown" and before.cleanup_status not in {"confirmed", "not_needed"}:
            raise RuntimeError("Execution cannot finish with unconfirmed cleanup")
        if state == "completed" and before.cleanup_status != "confirmed":
            raise RuntimeError("Completed tests require confirmed cleanup")
        record = ExecutionRecord.model_validate({**before.model_dump(), **changes, "state": state,
            "phase": "finished", "finished_at": datetime.now(UTC).isoformat()})
        self._save(record, "execution.finished")
        return record

    def attach_artifact(self, execution_id: str, artifact_id: str) -> None:
        record = self.get(execution_id)
        if record.state != "completed" or record.artifact_id is not None:
            raise RuntimeError("Only a completed execution can publish one public log")
        self._save(ExecutionRecord.model_validate({**record.model_dump(), "artifact_id": artifact_id}),
                   "execution.artifact_linked")

    def current(self, execution_id: str) -> ExecutionRecord:
        """Controller access; model-facing callers use get/status for visibility checks."""
        record = self._records.get(execution_id)
        if record is None or record.run_id != self.run_id:
            raise ToolFailure("EXECUTION_NOT_FOUND", "No such execution in the current process/run")
        return record.model_copy(deep=True)

    def get(self, execution_id: str) -> ExecutionRecord:
        record = self.current(execution_id)
        if record.scope != "public_tests":
            raise ToolFailure("EXECUTION_DENIED", "Execution is not model-visible public test output")
        return record

    def status(self, execution_id: str, *, generation: int, source_hashes: dict[str, str]) -> ExecutionStatusData:
        record = self.get(execution_id)
        return ExecutionStatusData(**{name: getattr(record, name) for name in (
            "execution_id", "tool_call_id", "state", "cleanup_status", "source_generation",
            "input_manifest_sha256", "exit_code", "passed", "failed", "public_tests_passed",
            "artifact_id", "error_code")},
            stale=record.source_generation != generation or record.source_hashes != source_hashes)
