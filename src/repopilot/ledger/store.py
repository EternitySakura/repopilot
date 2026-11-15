"""Synchronous intent-before-effect persistence; no execution replay or recovery."""

import hashlib
import json
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from repopilot.contracts.execution import (
    EXECUTION_ACTIONS,
    FINAL_VERIFICATION_MODE,
    ExecutionRecord,
)
from repopilot.ledger.config import LedgerSettings
from repopilot.ledger.schema_v1 import (
    artifacts,
    calls,
    events,
    executions,
    runs,
    tasks,
    test_results,
)

REVISION = "0001_initial"
_active: ContextVar = ContextVar("repopilot_ledger", default=None)
TERMINAL_EXECUTIONS = {"completed", "failed", "timed_out", "unknown"}


class LedgerError(RuntimeError):
    pass


class LedgerConflict(LedgerError):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def database_error(exc: SQLAlchemyError) -> LedgerError:
    # Driver text/parameters/DSNs may contain passwords. Keep a safe diagnostic code.
    state = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return LedgerError(f"Database operation failed ({type(exc).__name__}, SQLSTATE={state or 'unavailable'}); stop run")


def run_report_fields(row) -> dict:
    report = row["report"] or {}
    return {"stop_reason": report.get("stop_reason"), "duration_ms": report.get("duration_ms"),
            "max_run_seconds": report.get("limits", {}).get("max_run_seconds"),
            "candidate_assessment": report.get("candidate_assessment"),
            "accounting": report.get("accounting"), "artifact_directory": row["artifact_directory"]}


class Ledger:
    def __init__(self, url: str, *, read_only: bool = False):
        try:
            parsed = sa.engine.make_url(url)
            if parsed.drivername == "postgresql":
                parsed = parsed.set(drivername="postgresql+psycopg")
            if parsed.drivername not in {"sqlite", "postgresql+psycopg"}:
                raise LedgerError("Only SQLite or PostgreSQL with psycopg is supported")
            self.backend = parsed.get_backend_name()
            if self.backend == "sqlite" and parsed.database in {None, "", ":memory:"}:
                raise LedgerError("SQLite ledger requires a file-backed database")
            if self.backend == "sqlite":
                database = Path(parsed.database)
                if read_only:
                    if not database.is_file():
                        raise LedgerError("SQLite ledger file does not exist")
                    parsed = parsed.set(database=database.resolve().as_uri()).update_query_dict({"mode": "ro", "uri": "true"})
                else:
                    database.parent.mkdir(parents=True, exist_ok=True)
            connect_args = {"connect_timeout": 5} if self.backend == "postgresql" else {"timeout": 10}
            if self.backend == "postgresql" and read_only:
                options = parsed.query.get("options", "")
                connect_args["options"] = f"{options} -c default_transaction_read_only=on".strip()
            self.engine = sa.create_engine(parsed, hide_parameters=True, poolclass=NullPool,
                                           connect_args=connect_args)
            if self.backend == "sqlite":
                @sa.event.listens_for(self.engine, "connect")
                def sqlite_constraints(connection, _record):
                    connection.execute("PRAGMA foreign_keys=ON")
        except (ValueError, sa.exc.ArgumentError):
            raise LedgerError("Invalid database connection configuration") from None

    def close(self):
        self.engine.dispose()

    @contextmanager
    def transaction(self):
        try:
            with self.engine.connect() as connection:
                if self.backend == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    connection.begin()
                try:
                    yield connection
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except SQLAlchemyError as exc:
            raise database_error(exc) from None

    def migrate(self, revision: str = "head"):
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.transaction() as connection:
            if self.backend == "postgresql":
                connection.execute(sa.text("SELECT pg_advisory_xact_lock(7426102601)"))
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
        self.check_schema()

    def check_schema(self):
        try:
            with self.engine.connect() as connection:
                if not sa.inspect(connection).has_table("rp_alembic_version"):
                    raise LedgerError("Ledger not initialized; run repopilot --db-init")
                revision = connection.execute(sa.text("SELECT version_num FROM rp_alembic_version")).scalar()
                if revision != REVISION:
                    raise LedgerError("Unsupported ledger revision; migrate before starting a run")
        except SQLAlchemyError as exc:
            raise database_error(exc) from None

    def start_run(self, run_id: str, directory: Path):
        self.check_schema()
        with self.transaction() as connection:
            connection.execute(runs.insert().values(run_id=run_id, artifact_directory=str(directory),
                                                    created_at=datetime.now(UTC).isoformat()))

    def claim_final_verification(self, run_id: str, directory: Path, source_id: str):
        self.check_schema()
        with self.transaction() as connection:
            source = connection.execute(sa.select(runs).where(runs.c.run_id == source_id).with_for_update()).mappings().one()
            existing = connection.execute(sa.select(runs.c.run_id).where(runs.c.source_run_id == source_id,
                runs.c.mode == FINAL_VERIFICATION_MODE)).scalar_one_or_none()
            if existing is not None:
                raise LedgerConflict(f"Final verification is already claimed: {existing}")
            if source["status"] in {"running", "paused"} or source["task_sha256"] is None:
                raise LedgerConflict("Final verification requires a stopped source bound to a task")
            connection.execute(runs.insert().values(run_id=run_id, artifact_directory=str(directory),
                created_at=datetime.now(UTC).isoformat(), mode=FINAL_VERIFICATION_MODE, source_run_id=source_id,
                task_sha256=source["task_sha256"]))

    def bind_task(self, run_id: str, spec: dict):
        sha = digest(spec)
        with self.transaction() as connection:
            row = connection.execute(sa.select(runs).where(runs.c.run_id == run_id).with_for_update()).mappings().one()
            if row["task_sha256"] not in {None, sha}:
                raise LedgerConflict("Run task identity cannot change")
            insert = postgres_insert if self.backend == "postgresql" else sqlite_insert
            connection.execute(insert(tasks).values(spec_sha256=sha, task_key=spec["task_id"], spec=spec,
                repo_profile=spec["repo_profile"], base_revision=spec["base_revision"], created_at=datetime.now(UTC).isoformat()
            ).on_conflict_do_nothing(index_elements=["spec_sha256"]))
            exists = connection.execute(sa.select(tasks.c.spec).where(tasks.c.spec_sha256 == sha)).scalar_one()
            if exists != spec:
                raise LedgerConflict("Stored task differs from its content hash")
            connection.execute(runs.update().where(runs.c.run_id == run_id).values(task_sha256=sha))

    def append(self, record: dict, seq: int, *, transaction_connection=None):
        run_id = record["run_id"]
        sha = digest(record)
        with (self.transaction() if transaction_connection is None else nullcontext(transaction_connection)) as connection:
            run = connection.execute(sa.select(runs).where(runs.c.run_id == run_id).with_for_update()).mappings().one_or_none()
            if run is None:
                raise LedgerConflict("Event must belong to a registered run")
            old = connection.execute(sa.select(events.c.payload_sha256).where(
                events.c.run_id == run_id, events.c.seq_no == seq)).scalar()
            if old is not None:
                if old != sha:
                    raise LedgerConflict("Event sequence already belongs to different content")
                return  # Redelivery of an audit event, never permission to repeat its effect.
            if seq != run["last_seq"] + 1:
                raise LedgerConflict("Events must commit in order without sequence gaps")
            if run["status"] != "running" and record["event"] in {"tool.intent", "model.intent", "run.started", "run.finished"}:
                raise LedgerConflict("A finished run cannot restart actions")
            if record["event"] == "run.started":
                identity = (record.get("mode"), record.get("source_run_id"))
                claimed = (run["mode"], run["source_run_id"])
                if FINAL_VERIFICATION_MODE in (identity[0], claimed[0]) and identity != claimed:
                    raise LedgerConflict("Final verification must start from its original claim")
            connection.execute(events.insert().values(run_id=run_id, seq_no=seq, event_type=record["event"],
                occurred_at=record["timestamp"], payload_sha256=sha, payload=record))
            self._project(connection, record)
            connection.execute(runs.update().where(runs.c.run_id == run_id).values(last_seq=seq))

    def _project(self, connection, record):
        kind = record["event"]
        run_id = record["run_id"]
        call_id = record.get("tool_call_id")
        if kind == "run.started":
            connection.execute(runs.update().where(runs.c.run_id == run_id).values(
                mode=record.get("mode"), source_run_id=record.get("source_run_id")))
        elif kind == "run.resumed":
            before = connection.execute(sa.select(runs.c.status).where(runs.c.run_id == run_id)).scalar_one()
            latest = connection.execute(sa.select(events.c.payload).where(events.c.run_id == run_id,
                events.c.event_type == "run.checkpointed").order_by(events.c.seq_no.desc())).scalars().first()
            pending = connection.execute(sa.select(sa.func.count()).select_from(calls).where(calls.c.run_id == run_id,
                calls.c.status.in_(["intent", "unknown"]))).scalar_one()
            if before != "paused" or latest is None or latest["checkpoint_sha256"] != record["checkpoint_sha256"] or pending:
                raise LedgerConflict("Only a clean, ledger-bound paused checkpoint can resume")
            connection.execute(runs.update().where(runs.c.run_id == run_id).values(status="running", report=None, finished_at=None))
        elif kind == "run.finished":
            connection.execute(runs.update().where(runs.c.run_id == run_id).values(status=record["status"],
                finished_at=record["timestamp"], workspace_generation=record.get("workspace_generation", 0),
                report=record.get("report", record)))
        elif kind in {"tool.intent", "model.intent"}:
            model = kind == "model.intent"
            gate = record.get("action_type") == "gate_a_public_tests"
            connection.execute(calls.insert().values(run_id=run_id, call_id=call_id,
                kind="model" if model else "gate_a" if gate else "tool", name=record.get("model") if model else record.get("action_type") or "invalid",
                status="intent", intent_sha256=digest(record), arguments=record.get("action"),
                created_at=record["timestamp"], reserved_cost_usd=Decimal(record["reserved_cost_usd"]) if model else None,
                usage_status="unknown" if model else None))
        elif kind in {"tool.completed", "tool.failed", "model.completed", "model.failed"}:
            before = connection.execute(sa.select(calls).where(calls.c.run_id == run_id, calls.c.call_id == call_id)).mappings().one_or_none()
            if before is None or before["status"] != "intent":
                raise LedgerConflict("Call completion requires one unresolved intent in the same run")
            model = kind.startswith("model.")
            result = record.get("result", {})
            failed = kind.endswith("failed")
            execution_state = connection.execute(sa.select(executions.c.state).where(
                executions.c.run_id == run_id, executions.c.call_id == call_id)).scalar()
            status = "unknown" if execution_state == "unknown" or record.get("outcome_unknown") else "failed" if failed else "completed"
            values = {"status": status, "completed_at": record["timestamp"], "result": record if model else result or record,
                      "error_code": record.get("code") if model else (result.get("error") or {}).get("code") or record.get("code")}
            if model and record.get("usage") is not None:
                usage = record["usage"]
                values.update(usage_status="reported", prompt_tokens=usage.get("prompt_tokens"),
                              completion_tokens=usage.get("completion_tokens"),
                              cost_upper_bound_usd=Decimal(record["cost_upper_bound_usd"]))
            connection.execute(calls.update().where(calls.c.run_id == run_id, calls.c.call_id == call_id).values(**values))
        elif kind.startswith("execution."):
            item = ExecutionRecord.model_validate(record["execution_record"])
            if item.run_id != run_id or item.tool_call_id != call_id:
                raise LedgerConflict("Execution belongs to a different run/call")
            producer = connection.execute(sa.select(calls.c.name).where(calls.c.run_id == run_id, calls.c.call_id == call_id)).scalar()
            if producer != EXECUTION_ACTIONS[item.scope]:
                raise LedgerConflict("Execution scope does not match its producer")
            where = (executions.c.run_id == run_id, executions.c.execution_id == item.execution_id)
            before = connection.execute(sa.select(executions).where(*where)).mappings().one_or_none()
            values = {"run_id": run_id, "execution_id": item.execution_id, "call_id": call_id, "scope": item.scope,
                      "state": item.state, "cleanup_status": item.cleanup_status, "source_generation": item.source_generation,
                      "input_manifest_sha256": item.input_manifest_sha256, "record": item.model_dump()}
            if kind == "execution.prepared":
                connection.execute(executions.insert().values(**values))
            else:
                if before is None or any(before[key] != values[key] for key in ("call_id", "scope", "source_generation", "input_manifest_sha256")):
                    raise LedgerConflict("Execution input identity cannot change")
                if before["record"]["source_hashes"] != item.source_hashes:
                    raise LedgerConflict("Execution source identity cannot change")
                if before["state"] in TERMINAL_EXECUTIONS:
                    expected = {**before["record"], "artifact_id": item.artifact_id}
                    if (kind != "execution.artifact_linked" or before["state"] != "completed"
                            or before["record"]["artifact_id"] is not None or item.artifact_id is None
                            or expected != item.model_dump()):
                        raise LedgerConflict("Terminal execution cannot transition or repeat")
                elif before["state"] == "running" and item.state == "preparing":
                    raise LedgerConflict("Running execution cannot return to preparation")
                connection.execute(executions.update().where(*where).values(**values))
            if kind == "execution.finished":
                connection.execute(test_results.insert().values(run_id=run_id, execution_id=item.execution_id,
                    call_id=call_id, input_manifest_sha256=item.input_manifest_sha256, state=item.state,
                    exit_code=item.exit_code, passed=item.passed, failed=item.failed,
                    public_tests_passed=item.public_tests_passed, summary=item.model_dump()))
        elif kind == "artifact.registered":
            item = record["artifact_record"]
            if item["run_id"] != run_id:
                raise LedgerConflict("Artifact belongs to a different run")
            fields = {name: item.get(name) for name in ("artifact_id", "execution_id", "producer_call_id",
                "input_manifest_sha256", "relative_path", "visibility", "kind", "sha256", "size_bytes")}
            connection.execute(artifacts.insert().values(run_id=run_id, record=item, **fields))

    def list_runs(self, *, limit: int = 20, task_id: str | None = None, status: str | None = None) -> dict:
        """Read recent runs and their persisted stop reasons without resuming work."""
        if not 1 <= limit <= 100:
            raise ValueError("Run listing limit must be between 1 and 100")
        pending = sa.select(sa.func.count()).select_from(calls).where(
            calls.c.run_id == runs.c.run_id, calls.c.status.in_(["intent", "unknown"])).scalar_subquery()
        checkpoint = sa.exists(sa.select(1).where(events.c.run_id == runs.c.run_id, events.c.event_type == "run.checkpointed"))
        query = (sa.select(runs, tasks.c.task_key.label("task_id"), pending.label("unresolved_calls"), checkpoint.label("checkpoint_available"))
                 .outerjoin(tasks, runs.c.task_sha256 == tasks.c.spec_sha256)
                 .order_by(runs.c.created_at.desc(), runs.c.run_id.desc()).limit(limit + 1))
        if task_id is not None:
            query = query.where(tasks.c.task_key == task_id)
        if status is not None:
            query = query.where(runs.c.status == status)
        try:
            with self.engine.connect() as connection:
                records = connection.execute(query).mappings().all()
        except SQLAlchemyError as exc:
            raise database_error(exc) from None
        items = []
        for row in records[:limit]:
            items.append({
                "checkpoint_available": bool(row["checkpoint_available"]),
                **{key: row[key] for key in ("run_id", "task_id", "status", "mode", "source_run_id",
                                            "created_at", "finished_at", "unresolved_calls")},
                **run_report_fields(row),
            })
        return {"backend": self.backend, "runs": items, "limit": limit, "truncated": len(records) > limit,
                "filters": {"task_id": task_id, "status": status},
                "recovery_supported": any(item["status"] == "paused" and item["checkpoint_available"] for item in items)}

    def summary(self, run_id: str) -> dict:
        try:
            with self.engine.connect() as connection:
                run = connection.execute(sa.select(runs, tasks.c.task_key.label("task_id"))
                    .outerjoin(tasks, runs.c.task_sha256 == tasks.c.spec_sha256)
                    .where(runs.c.run_id == run_id)).mappings().one_or_none()
                if run is None:
                    raise LedgerError("Run not found in this ledger")
                counts = {table.name.removeprefix("rp_"): connection.execute(sa.select(sa.func.count()).select_from(table).where(
                    table.c.run_id == run_id)).scalar_one() for table in (events, calls, executions, test_results, artifacts)}
                pending = connection.execute(sa.select(calls.c.call_id, calls.c.status).where(
                    calls.c.run_id == run_id, calls.c.status.in_(["intent", "unknown"]))).mappings().all()
                resumable = run["status"] == "paused" and run["mode"] == "live_candidate" and bool(connection.execute(
                    sa.select(sa.func.count()).select_from(events).where(events.c.run_id == run_id,
                    events.c.event_type == "run.checkpointed")).scalar_one())
                return {"run_id": run_id, "backend": self.backend, "status": run["status"], "mode": run["mode"],
                        "task_id": run["task_id"], **run_report_fields(run),
                        "task_sha256": run["task_sha256"], "counts": counts,
                        "unresolved_calls": [dict(row) for row in pending], "recovery_supported": resumable}
        except SQLAlchemyError as exc:
            raise database_error(exc) from None


@contextmanager
def using_ledger(ledger: Ledger):
    token = _active.set(ledger)
    try:
        yield ledger
    finally:
        _active.reset(token)


def configured_ledger() -> Ledger | None:
    active = _active.get()
    if active is not None:
        return active
    settings = LedgerSettings()
    return Ledger(settings.url.get_secret_value()) if settings.enabled else None
