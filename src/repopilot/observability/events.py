import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from repopilot.contracts.evaluation import EvaluationContext
from repopilot.ledger.store import configured_ledger


class EventLog:
    def __init__(self, runs: Path, *, final_source_run_id: str | None = None):
        self.run_id = uuid4().hex
        self.directory = (runs / self.run_id).absolute()
        self.path = self.directory / "events.jsonl"
        self.final_status: str | None = None
        self.model_failure: dict | None = None
        self.evaluation_context: EvaluationContext | None = None
        self.ledger = configured_ledger()
        self.sequence = 0
        if final_source_run_id is not None:
            if self.ledger is None:
                raise ValueError("Final verification requires a ledger")
            self.ledger.claim_final_verification(self.run_id, self.directory, final_source_run_id)
        self.directory.mkdir(parents=True, exist_ok=False)
        if self.ledger is not None and final_source_run_id is None:
            self.ledger.start_run(self.run_id, self.directory)

    def register_task(self, task):
        if self.ledger is not None:
            self.ledger.bind_task(self.run_id, task.model_dump(mode="json"))

    @classmethod
    def attach_paused(cls, ledger, run_id: str, directory: Path, sequence: int, evaluation_context=None):
        log = cls.__new__(cls)
        log.run_id, log.directory = run_id, directory
        log.path = directory / "events.jsonl"
        log.final_status, log.model_failure = "paused", None
        log.evaluation_context = evaluation_context
        log.ledger, log.sequence = ledger, sequence
        return log

    def bind_evaluation(self, context: EvaluationContext):
        if self.evaluation_context is not None:
            raise ValueError("Evaluation binding cannot change")
        self.write("evaluation.bound", context=context.model_dump(mode="json"))
        self.evaluation_context = context.model_copy(deep=True)

    def write(self, event: str, *, tool_call_id=None, action_type=None, **fields):
        record = {
            "event": event, "run_id": self.run_id,
            "tool_call_id": str(tool_call_id) if tool_call_id else None,
            "timestamp": datetime.now(UTC).isoformat(),
            "action_type": action_type, **fields,
        }
        # Commit the intent before any tool/provider side effect. PostgreSQL and the
        # JSONL file are separate resources; a failure here never authorizes replay.
        if self.ledger is not None:
            self.ledger.append(record, self.sequence + 1)
        self.sequence += 1
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
        if event == "run.finished":
            self.final_status = fields.get("status")
        elif event == "run.resumed":
            self.final_status = None
        elif event == "model.failed":
            self.model_failure = {"tool_call_id": record["tool_call_id"], "code": record["code"],
                                  "duration_ms": record.get("duration_ms"),
                                  "response_diagnostic": record.get("response_diagnostic")}
