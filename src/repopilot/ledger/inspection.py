"""Read persisted incomplete evidence and optional Docker inventories; never replay."""

import hashlib
import json
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from repopilot.contracts.execution import FINAL_VERIFICATION_MODE, ExecutionRecord
from repopilot.ledger.audit import audit_run, inspect_change_sets, read_bytes
from repopilot.ledger.checkpoints import inspect_checkpoints
from repopilot.ledger.imports import inspect_import
from repopilot.ledger.schema_v1 import calls, events, executions, runs
from repopilot.ledger.store import Ledger, LedgerError, database_error, digest
from repopilot.ledger.verifications import inspect_verifications
from repopilot.tools.errors import ToolFailure
from repopilot.tools.testing import DockerTests


def resource_inventory(resources: dict[str, str]) -> dict:
    result = {}
    for kind, name in resources.items():
        query = (["container", "ls", "--all", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"]
                 if kind == "container" else
                 ["image", "ls", "--all", "--filter", f"reference={name}", "--format", "{{.Repository}}:{{.Tag}}"])
        try:
            inventory = DockerTests.command(query, timeout=10)
        except ToolFailure as exc:
            result[kind] = {"name": name, "state": "unknown", "error_code": exc.code}
        else:
            result[kind] = {"name": name, "state": "unknown" if inventory.returncode else
                            "present" if inventory.stdout.strip() else "absent"}
    return result


def inspect_run(ledger: Ledger, run_id: str, *, check_docker: bool = False) -> dict:
    try:
        with ledger.engine.connect() as connection:
            run = connection.execute(sa.select(runs).where(runs.c.run_id == run_id)).mappings().one_or_none()
            if run is None:
                raise LedgerError("Run not found in this ledger")
            recorded_events = connection.execute(sa.select(events).where(events.c.run_id == run_id)
                                                 .order_by(events.c.seq_no)).mappings().all()
            pending = connection.execute(sa.select(calls.c.call_id, calls.c.kind, calls.c.name, calls.c.status,
                calls.c.usage_status, calls.c.reserved_cost_usd).where(
                    calls.c.run_id == run_id, calls.c.status.in_(["intent", "unknown"]))).mappings().all()
            records = connection.execute(sa.select(executions.c.record).where(
                executions.c.run_id == run_id)).scalars().all()
            final = run if run["mode"] == FINAL_VERIFICATION_MODE else connection.execute(sa.select(runs).where(
                runs.c.source_run_id == run_id, runs.c.mode == FINAL_VERIFICATION_MODE)).mappings().one_or_none()
    except SQLAlchemyError as exc:
        raise database_error(exc) from None

    directory = Path(run["artifact_directory"])
    issues, file_events = [], []
    raw = None
    try:
        raw = read_bytes(directory, "events.jsonl")
    except FileNotFoundError:
        issues.append("EVENT_FILE_MISSING")
    else:
        for number, line in enumerate(raw.splitlines(), 1):
            try:
                file_events.append(json.loads(line))
            except (ValueError, UnicodeError):
                issues.append(f"EVENT_JSON_INVALID_LINE_{number}")
                break
    if [row["payload"] for row in recorded_events] != file_events:
        issues.append("EVENT_PAYLOAD_MISMATCH")
    if ([row["seq_no"] for row in recorded_events] != list(range(1, len(recorded_events) + 1))
            or run["last_seq"] != len(recorded_events)):
        issues.append("EVENT_SEQUENCE_MISMATCH")
    if any(row["payload_sha256"] != digest(row["payload"]) for row in recorded_events):
        issues.append("EVENT_HASH_MISMATCH")

    report_name = "verification.json" if run["mode"] in {"cleanroom_public_gate_a", FINAL_VERIFICATION_MODE} else "run.json"
    try:
        file_report = json.loads(read_bytes(directory, report_name))
    except FileNotFoundError:
        file_report = None
        if run["status"] != "running" or run["report"] is not None:
            issues.append("RUN_REPORT_MISSING")
    except (ValueError, UnicodeError):
        file_report = None
        issues.append("RUN_REPORT_INVALID")
    if file_report is not None and not isinstance(file_report, dict):
        issues.append("RUN_REPORT_INVALID")
        file_report = None
    if file_report != run["report"]:
        issues.append("RUN_REPORT_MISMATCH")
    if file_report is not None and file_report.get("status") != run["status"]:
        issues.append("RUN_STATUS_MISMATCH")

    try:
        snapshot = json.loads(read_bytes(directory, "executions.json"))
        file_records = [ExecutionRecord.model_validate(item).model_dump() for item in snapshot["records"]]
        if snapshot["run_id"] != run_id or sorted(file_records, key=lambda item: item["execution_id"]) != sorted(
                records, key=lambda item: item["execution_id"]):
            issues.append("EXECUTION_SNAPSHOT_MISMATCH")
    except FileNotFoundError:
        if records:
            issues.append("EXECUTION_SNAPSHOT_MISSING")
    except (ValueError, UnicodeError, KeyError, TypeError):
        issues.append("EXECUTION_SNAPSHOT_INVALID")

    batches = inspect_change_sets(directory, file_events)
    issues.extend(batches["issues"])
    imported = None
    if run["mode"] == "imported_candidate":
        with ledger.engine.connect() as connection:
            has_calls = bool(connection.execute(sa.select(sa.func.count()).select_from(calls).where(calls.c.run_id == run_id)).scalar_one())
        imported = inspect_import(directory, file_events, file_report, run, has_calls=has_calls)
        issues.extend(imported["issues"])
    verification = inspect_verifications(ledger, run)
    final_status = None
    if final is not None:
        terminal = final["report"] or {}
        final_issues = []
        if final["status"] != "running":
            try:
                checked = audit_run(ledger, Path(final["artifact_directory"]))
                final_issues = checked["issues"]
                if checked["unresolved_calls"]:
                    final_issues.append("FINAL_GATE_CALL_UNRESOLVED")
            except (OSError, ValueError, KeyError, TypeError, ToolFailure):
                final_issues = ["FINAL_GATE_EVIDENCE_INVALID"]
        if final["run_id"] == run_id:
            issues.extend(final_issues)
        final_status = {"verification_id": final["run_id"], "status": final["status"],
                        "reason": terminal.get("reason"), "public_gate_id": terminal.get("public_gate_id"),
                        "evidence_consistent": final["status"] != "running" and not final_issues,
                        "issues": final_issues, "replay_supported": False}
    checkpoints = inspect_checkpoints(directory, file_events, raw or b"")
    issues.extend(checkpoints["issues"])
    resume = {"available": False, "reason": "NOT_PAUSED", "remaining": None}
    if run["status"] == "paused":
        resume["reason"] = "EVIDENCE_MISMATCH"
        if not issues:
            from repopilot.agent.checkpoints import (
                checkpoint_budget,
                verified_checkpoint,
            )
            try:
                envelope, _, report, _, _ = verified_checkpoint(ledger, run_id)
            except (ValueError, FileNotFoundError, ToolFailure):
                resume["reason"] = "CHECKPOINT_NOT_RESUMABLE"
            else:
                remaining = checkpoint_budget(envelope.checkpoint, report)
                exhausted = (remaining["remaining_model_calls"] <= 0 or remaining["remaining_decisions"] <= 0
                             or remaining["remaining_tool_calls"] <= 0 or float(remaining["remaining_cost_cap_usd"]) <= 0
                             or remaining["remaining_active_seconds"] == 0)
                resume.update(available=not exhausted, reason="BUDGET_EXHAUSTED" if exhausted else None,
                              remaining=remaining)
    batch_attention = any(not item["complete"] or item["status"] == "rollback_failed" for item in batches["change_sets"])
    observed = []
    for record in records:
        item = {key: record[key] for key in ("execution_id", "tool_call_id", "state", "cleanup_status", "resources")}
        if check_docker:
            item["resource_inventory"] = resource_inventory(record["resources"])
        observed.append(item)
    incomplete = (run["status"] in {"running", "paused"} or bool(pending) or file_report is None
                  or any(not item["complete"] for item in batches["change_sets"])
                  or any(item["state"] in {"preparing", "running", "unknown"}
                         or item["cleanup_status"] in {"pending", "unknown"} for item in observed))
    leftovers = any(resource["state"] != "absent" for item in observed
                    for resource in item.get("resource_inventory", {}).values())
    verification["source_evidence_consistent"] = not issues
    if issues:
        verification["matching_passed_gate_ids"] = []
    if final_status is not None and final["run_id"] != run_id:
        final_status["candidate_matches"] = final_status["public_gate_id"] in verification["matching_passed_gate_ids"]
    return {
        "run_id": run_id, "status": run["status"], "mode": run["mode"],
        "source_run_id": run["source_run_id"], "artifact_directory": str(directory),
        "events_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "evidence_consistent": not issues, "issues": issues, "incomplete": incomplete,
        "needs_attention": (bool(issues) or incomplete or leftovers or batch_attention
                            or (final_status is not None and not final_status["evidence_consistent"])
                            or any(not item["evidence_consistent"] for item in verification["history"])),
        "import": imported, "public_verification": verification, "final_verification": final_status,
        "change_sets": batches["change_sets"],
        "checkpoints": checkpoints["history"], "resume": resume,
        "pending_calls": [{**dict(row), "reserved_cost_usd": str(row["reserved_cost_usd"])
                           if row["reserved_cost_usd"] is not None else None} for row in pending],
        "executions": observed, "docker_checked": check_docker,
        "automatic_resume": False, "side_effects": [],
    }
