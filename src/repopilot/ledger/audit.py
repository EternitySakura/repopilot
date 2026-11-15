"""Read-only reconciliation of current-run files, JSONL and relational records."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path, PurePosixPath

import sqlalchemy as sa
from pydantic import ValidationError

from repopilot.agent.deepseek import cost_bound
from repopilot.contracts.execution import FINAL_VERIFICATION_MODE
from repopilot.contracts.model_response import DIAGNOSTIC_VERSIONS, ResponseDiagnostic
from repopilot.contracts.reconstruction import ReconstructionEnvelope
from repopilot.ledger.schema_v1 import (
    artifacts,
    calls,
    events,
    executions,
    runs,
    test_results,
)
from repopilot.ledger.store import Ledger, digest
from repopilot.tools.errors import ToolFailure
from repopilot.tools.policy import ReadPolicy

MAX_AUDIT_BYTES = 16 * 1024 * 1024


def read_bytes(root: Path, relative: str) -> bytes:
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts or "\\" in relative or ":" in relative:
        raise ValueError("Audit path must be relative to its run")
    path = root / relative
    for node in (*reversed(path.parents), path):
        ReadPolicy._check_node(node)
    with path.open("rb") as stream:
        data = stream.read(MAX_AUDIT_BYTES + 1)
    if len(data) > MAX_AUDIT_BYTES:
        raise ValueError("Audit artifact exceeds limit")
    return data


def inspect_change_sets(directory: Path, file_events: list[dict], *, secret: str | None = None) -> dict:
    """Shared, read-only checks for complete batches and prepared/interrupted journals."""
    issues, batches = [], []
    intents = {event["tool_call_id"]: event for event in file_events if event["event"] == "tool.intent"}

    def check(condition, code):
        if not condition and code not in issues:
            issues.append(code)

    for started in (event for event in file_events if event["event"] == "edit.batch_started"):
        call_id = started["tool_call_id"]
        entry = {"tool_call_id": call_id, "status": "unknown", "complete": False, "files": []}
        batches.append(entry)
        if call_id not in intents or intents[call_id].get("action_type") != "edit_files":
            check(False, "BATCH_EDIT_ID_INVALID")
            continue
        prefix = f"edits/{call_id}"
        try:
            raw = read_bytes(directory, prefix + "/journal.json")
            journal = json.loads(raw)
            check(secret is None or secret not in raw.decode("utf-8"), "SECRET_IN_BATCH_EDIT")
            finished = [event for event in file_events if event["event"] == "edit.batch_finished" and event["tool_call_id"] == call_id]
            check(journal["status"] in {"prepared", "committed", "rolled_back", "rollback_failed"}, "BATCH_EDIT_STATUS_INVALID")
            check(journal["files"] == started["files"] and journal["tool_call_id"] == call_id,
                  "BATCH_EDIT_MANIFEST_MISMATCH")
            check(len(finished) <= 1, "BATCH_EDIT_TERMINAL_MISMATCH")
            if len(finished) == 1:
                check(finished[0]["journal_sha256"] == hashlib.sha256(raw).hexdigest()
                      and finished[0]["status"] == journal["status"], "BATCH_EDIT_JOURNAL_MISMATCH")
            for item in journal["files"]:
                for name, key in (("before", "before_sha256"), ("after", "after_sha256")):
                    if item[key] is not None:
                        content = read_bytes(directory, f"{prefix}/{item['index']}/{name}.bin")
                        check(hashlib.sha256(content).hexdigest() == item[key], "BATCH_EDIT_CONTENT_MISMATCH")
                        check(secret is None or secret not in content.decode("utf-8", errors="replace"), "SECRET_IN_BATCH_EDIT")
            entry.update(status=journal["status"], complete=len(finished) == 1 and journal["status"] != "prepared",
                         files=journal["files"], generation_before=journal["generation_before"],
                         generation_after=journal["generation_after"], journal_ref=prefix + "/journal.json",
                         journal_sha256=hashlib.sha256(raw).hexdigest())
        except (OSError, ToolFailure, ValueError, KeyError, TypeError):
            check(False, "BATCH_EDIT_JOURNAL_UNREADABLE")
    return {"change_sets": batches, "issues": issues}


def final_event_suffix(file_events: list[dict], db_events: list[dict], report: dict) -> tuple[dict | None, str | None]:
    """Recognize only a single, matching final report event missing on one side."""
    missing = None
    if file_events == db_events:
        tail = db_events[-1] if db_events else None
        if tail is None or tail["event"] != "run.finished":
            return None, None
    elif len(file_events) == len(db_events) + 1 and file_events[:-1] == db_events:
        tail, missing = file_events[-1], "ledger"
    elif len(db_events) == len(file_events) + 1 and db_events[:-1] == file_events:
        tail, missing = db_events[-1], "file"
    else:
        raise ValueError("Final verification event mirrors diverge")
    if (tail["event"] != "run.finished" or tail["report"] != report or tail["status"] != report["status"]
            or tail["run_id"] != report["verification_id"] or tail["tool_call_id"] is not None):
        raise ValueError("Final verification tail does not match the recovered report")
    return tail, missing


def audit_run(ledger: Ledger, directory: Path, *, secret: str | None = None, pending_final: dict | None = None) -> dict:
    issues = []

    def absent(text):
        return secret is None or secret not in text

    def check(condition, code):
        if not condition and code not in issues:
            issues.append(code)

    with ledger.engine.connect() as connection:
        data = {table.name.removeprefix("rp_"): [dict(row) for row in connection.execute(sa.select(table).where(
            table.c.run_id == directory.name)).mappings()] for table in (calls, events, executions, test_results, artifacts)}
        run = connection.execute(sa.select(runs).where(runs.c.run_id == directory.name)).mappings().one()
    if run["mode"] == FINAL_VERIFICATION_MODE:
        raise ToolFailure("PRIVATE_VERIFICATION_UNAVAILABLE", "Private Gate B requires the controller edition")
    raw = read_bytes(directory, "events.jsonl")
    file_events = [json.loads(line) for line in raw.decode().splitlines()]
    db_events = sorted(data["events"], key=lambda row: row["seq_no"])
    if pending_final is not None:
        if run["mode"] != FINAL_VERIFICATION_MODE:
            raise ValueError("Pending report audit requires a final verification run")
        _, missing = final_event_suffix(file_events, [row["payload"] for row in db_events], pending_final)
        if missing is not None:
            file_events = [row["payload"] for row in db_events]
    check([row["seq_no"] for row in db_events] == list(range(1, len(file_events) + 1)), "EVENT_SEQUENCE_MISMATCH")
    check([row["payload"] for row in db_events] == file_events, "EVENT_PAYLOAD_MISMATCH")
    check(all(row["payload_sha256"] == digest(row["payload"]) for row in db_events), "EVENT_HASH_MISMATCH")
    check(run["last_seq"] == len(file_events), "RUN_SEQUENCE_MISMATCH")
    check(absent(raw.decode()) and absent(json.dumps(data, default=str)), "SECRET_IN_AUDIT_DATA")
    report_name = "run.json" if (directory / "run.json").exists() else "verification.json"
    report_raw = read_bytes(directory, report_name) if pending_final is None else json.dumps(pending_final).encode()
    report = json.loads(report_raw)
    check(absent(report_raw.decode()) and absent(json.dumps(dict(run), default=str)), "SECRET_IN_RUN_REPORT")
    check((pending_final is not None and run["status"] == "running" and run["report"] is None)
          or (run["report"] == report and run["status"] == report["status"]), "RUN_REPORT_MISMATCH")
    check(run["task_sha256"] is not None, "MISSING_TASK_VERSION")
    bindings = [(i, row) for i, row in enumerate(file_events) if row["event"] == "evaluation.bound"]
    context = report.get("evaluation_context")
    check(len(bindings) == (1 if context is not None else 0), "EVALUATION_BINDING_COUNT_MISMATCH")
    if context is not None and len(bindings) == 1:
        check(bindings[0][1].get("context") == context and context["task_id"] == report["task_id"], "EVALUATION_CONTEXT_MISMATCH")
        check(all(bindings[0][0] < i for i, row in enumerate(file_events)
                  if row["event"] in {"tool.intent", "model.intent"}), "EVALUATION_BINDING_ORDER_MISMATCH")
    if "model_failure" in report:
        failures = [row for row in file_events if row["event"] == "model.failed"]
        last = failures[-1] if failures else None
        expected = None if last is None else {"tool_call_id": last["tool_call_id"], "code": last["code"],
                                              "duration_ms": last.get("duration_ms"),
                                              "response_diagnostic": last.get("response_diagnostic")}
        check(report["model_failure"] == expected, "MODEL_FAILURE_DIAGNOSTIC_MISMATCH")
    if (directory / "task.json").exists():
        task = json.loads(read_bytes(directory, "task.json"))
        check(digest(task) == run["task_sha256"] and task["task_id"] == report["task_id"], "RUN_TASK_BINDING_MISMATCH")
    if run["mode"] == "cleanroom_public_gate_a":
        check(report["source_run_id"] == run["source_run_id"] and report["verification_id"] == directory.name,
              "GATE_IDENTITY_MISMATCH")
        gate_intents = [row for row in file_events if row["event"] == "verification.intent"]
        check(len(gate_intents) <= 1 and (not report["test_attempts"] or len(gate_intents) == 1), "GATE_INTENT_MISMATCH")
        if len(gate_intents) == 1:
            intent = gate_intents[0]
            check(intent["source_run_id"] == report["source_run_id"] and intent["scope"] == "public_gate_a"
                  and all(intent[key] == report[key] for key in ("patch_sha256", "manifest_sha256", "base_revision")),
                  "GATE_INTENT_MISMATCH")
            for name, key in (("candidate.patch", "patch_sha256"), ("candidate.manifest.json", "manifest_sha256")):
                try:
                    content = read_bytes(directory, "inputs/" + name)
                    check(hashlib.sha256(content).hexdigest() == report[key], "GATE_CANDIDATE_INPUT_MISMATCH")
                except FileNotFoundError:
                    check(False, "GATE_CANDIDATE_INPUT_MISSING")
    if run["mode"] == FINAL_VERIFICATION_MODE:
        bindings = [row for row in file_events if row["event"] == "verification.final_intent"]
        finished = [row for row in file_events if row["event"] == "run.finished"]
        if pending_final is not None and run["status"] == "running":
            check(not finished, "FINAL_GATE_TERMINAL_EVENT_MISMATCH")
        else:
            check(len(finished) == 1 and finished[0] == file_events[-1]
                  and finished[0].get("report") == report and finished[0].get("status") == report["status"],
                  "FINAL_GATE_TERMINAL_EVENT_MISMATCH")
        check(report["source_run_id"] == run["source_run_id"] and report["verification_id"] == directory.name,
              "FINAL_GATE_IDENTITY_MISMATCH")
        check(len(bindings) == 1 and all(bindings[0][key] == report[key] for key in
              ("source_run_id", "public_gate_id", "patch_sha256", "manifest_sha256", "private_suite_sha256", "harness_sha256")),
              "FINAL_GATE_BINDING_MISMATCH")
        check(report["model_requests"] == 0 and len(data["calls"]) == report["test_attempts"] <= 1
              and all(call["name"] == "gate_b_private_tests" for call in data["calls"]), "FINAL_GATE_CALL_MISMATCH")
        if data["executions"]:
            records = [row["record"] for row in data["executions"]]
            snapshot = json.loads(read_bytes(directory, "executions.json"))
            completed_executions = [row["execution_record"] for row in file_events if row["event"] == "execution.finished"]
            check(snapshot["run_id"] == directory.name and snapshot["records"] == completed_executions == records,
                  "FINAL_GATE_EXECUTION_EVIDENCE_MISMATCH")
        with ledger.engine.connect() as connection:
            public = connection.execute(sa.select(runs).where(runs.c.run_id == report["public_gate_id"])).mappings().one_or_none()
        check(public is not None and public["mode"] == "cleanroom_public_gate_a" and public["status"] == "passed"
              and public["source_run_id"] == run["source_run_id"] and public["task_sha256"] == run["task_sha256"]
              and (not report["source_hashes"] or report["source_hashes"] == public["report"]["source_hashes"])
              and all(public["report"][key] == report[key] for key in ("patch_sha256", "manifest_sha256")),
              "FINAL_GATE_PUBLIC_BINDING_MISMATCH")
        if report["status"] in {"succeeded", "unresolved"}:
            completed = [row["result"] for row in file_events if row["event"] == "tool.completed"]
            check(report["execution_id"] is not None and report["output_sha256"] is not None and len(completed) == 1
                  and all(completed[0][key] == report[key] for key in
                          ("execution_id", "output_sha256", "output_truncated", "status", "reason")),
                  "FINAL_GATE_RESULT_MISMATCH")
        for name, key in (("candidate.patch", "patch_sha256"), ("candidate.manifest.json", "manifest_sha256")):
            try:
                content = read_bytes(directory, "inputs/" + name)
                check(hashlib.sha256(content).hexdigest() == report[key], "FINAL_GATE_INPUT_MISMATCH")
            except FileNotFoundError:
                check(False, "FINAL_GATE_INPUT_MISSING")

    intents = {row["tool_call_id"]: (index, row) for index, row in enumerate(file_events)
               if row["event"] in {"model.intent", "tool.intent"}}
    check(set(intents) == {row["call_id"] for row in data["calls"]}, "CALL_ID_MISMATCH")
    model_cost = Decimal(0)
    prompt_tokens = completion_tokens = unknown_usage = model_count = 0
    for call in data["calls"]:
        matches = [(i, row) for i, row in enumerate(file_events) if row["tool_call_id"] == call["call_id"]
                   and row["event"] in {"tool.completed", "tool.failed", "model.completed", "model.failed"}]
        intent = intents.get(call["call_id"])
        check(intent is not None and len(matches) == 1, "INCOMPLETE_CALL")
        if intent is None or len(matches) != 1:
            continue
        terminal_index, terminal = matches[0]
        check(intent[0] < terminal_index, "CALL_ORDER_MISMATCH")
        check(call["intent_sha256"] == digest(intent[1]), "CALL_INTENT_HASH_MISMATCH")
        model = call["kind"] == "model"
        expected_result = terminal if model else terminal.get("result") or terminal
        check(call["result"] == expected_result, "CALL_RESULT_MISMATCH")
        if not model and terminal.get("result", {}).get("tool_call_id") is not None:
            check(terminal["result"]["tool_call_id"] == call["call_id"], "TOOL_RETURN_ID_MISMATCH")
        if model:
            model_count += 1
            diagnostic = terminal.get("response_diagnostic")
            if diagnostic is not None:
                try:
                    parsed = ResponseDiagnostic.model_validate(diagnostic)
                except ValidationError:
                    check(False, "MODEL_RESPONSE_DIAGNOSTIC_INVALID")
                else:
                    check(parsed.parser_version == intent[1].get("parser_version"), "MODEL_RESPONSE_DIAGNOSTIC_VERSION_MISMATCH")
                    policy_version = DIAGNOSTIC_VERSIONS.get(intent[1].get("diagnostic_policy"))
                    if policy_version is not None:
                        check(parsed.version == policy_version, "MODEL_DIAGNOSTIC_POLICY_MISMATCH")
                    if policy_version is not None and terminal["event"] == "model.completed":
                        check(parsed.choice_count == 1 and parsed.finish_reason == "stop" and parsed.content_type == "str"
                              and parsed.starts_object is True and parsed.content_sha256 is not None
                              and parsed.content_bytes is not None and parsed.json_error_kind is None,
                              "MODEL_RESPONSE_DIAGNOSTIC_INCONSISTENT")
            if intent[1].get("diagnostic_policy") in DIAGNOSTIC_VERSIONS and terminal["event"] == "model.completed":
                check(diagnostic is not None, "MODEL_RESPONSE_DIAGNOSTIC_MISSING")
            reservation = Decimal(intent[1]["reserved_cost_usd"])
            check(call["reserved_cost_usd"] == reservation, "MODEL_RESERVATION_MISMATCH")
            usage = terminal.get("usage")
            if usage is None:
                unknown_usage += 1
                model_cost += reservation
                check(call["usage_status"] == "unknown" and call["prompt_tokens"] is None
                      and call["completion_tokens"] is None and call["cost_upper_bound_usd"] is None, "UNKNOWN_USAGE_REPORTED_AS_ZERO")
            else:
                prompt_tokens += usage["prompt_tokens"]
                completion_tokens += usage["completion_tokens"]
                cost = cost_bound(usage["prompt_tokens"], usage["completion_tokens"], usage.get("prompt_cache_hit_tokens") or 0)
                model_cost += cost
                check(call["usage_status"] == "reported" and call["prompt_tokens"] == usage["prompt_tokens"]
                      and call["completion_tokens"] == usage["completion_tokens"], "MODEL_USAGE_MISMATCH")
                check(call["cost_upper_bound_usd"] == cost == Decimal(terminal["cost_upper_bound_usd"]), "MODEL_COST_MISMATCH")
    if "accounting" in report:
        accounting = report["accounting"]
        if accounting["usage_status"] == "not_applicable":
            check(model_count == 0, "MODEL_ACCOUNTING_MISSING")
        else:
            check(accounting["requests"] == model_count, "MODEL_COUNT_MISMATCH")
            check(accounting["known_prompt_tokens"] == prompt_tokens and accounting["known_completion_tokens"] == completion_tokens,
                  "MODEL_TOKEN_TOTAL_MISMATCH")
            check(accounting["unknown_usage_calls"] == unknown_usage, "MODEL_UNKNOWN_TOTAL_MISMATCH")
            check(Decimal(accounting["cost_upper_bound_usd"]) == model_cost, "MODEL_COST_TOTAL_MISMATCH")
            check(model_cost <= Decimal(accounting["max_cost_usd"]), "MODEL_BUDGET_EXCEEDED")
        check(report["counters"]["tool_calls"] == sum(row["kind"] == "tool" for row in data["calls"]), "TOOL_COUNT_MISMATCH")
        if run["mode"] == "holdout_reference":
            check(report["mode"] == "holdout_reference" and not data["calls"]
                  and accounting["usage_status"] == "not_applicable"
                  and all(value == 0 for value in report["counters"].values()), "HOLDOUT_REFERENCE_CALL_MISMATCH")
        else:
            tools = json.loads(read_bytes(directory, "tools.json"))
            check(absent(json.dumps(tools)), "SECRET_IN_TOOL_SCHEMA")
            schema_sha = hashlib.sha256(json.dumps(tools["action_schema"], sort_keys=True).encode()).hexdigest()
            check(schema_sha == tools["schema_sha256"], "TOOL_SCHEMA_HASH_MISMATCH")
            check(all(row[1]["schema_sha256"] == schema_sha and row[1]["enabled_tools"] == tools["enabled_tools"]
                      for row in intents.values() if row[1]["event"] == "model.intent"), "MODEL_TOOL_SCOPE_MISMATCH")

    execution_map = {row["execution_id"]: row for row in data["executions"]}
    result_map = {row["execution_id"]: row for row in data["test_results"]}
    check(set(execution_map) == set(result_map), "EXECUTION_RESULT_SET_MISMATCH")
    for identity, execution in execution_map.items():
        item = execution["record"]
        manifest_raw = read_bytes(directory, f"tests/{item['tool_call_id']}/input.manifest.json")
        manifest = json.loads(manifest_raw)
        if "task_id" in manifest:
            check(manifest["task_id"] == report["task_id"], "EXECUTION_TASK_MISMATCH")
        check(absent(manifest_raw.decode()), "SECRET_IN_INPUT_MANIFEST")
        check(hashlib.sha256(manifest_raw).hexdigest() == execution["input_manifest_sha256"] == item["input_manifest_sha256"],
              "EXECUTION_INPUT_HASH_MISMATCH")
        check(manifest["sources"] == item["source_hashes"] and manifest["source_generation"] == item["source_generation"],
              "EXECUTION_SOURCE_MISMATCH")
        check(item["run_id"] == directory.name and execution["call_id"] == item["tool_call_id"], "EXECUTION_CALL_MISMATCH")
        check(execution["state"] == item["state"] and execution["cleanup_status"] == item["cleanup_status"], "EXECUTION_STATE_MISMATCH")
        if item["state"] != "unknown":
            check(item["cleanup_status"] in {"confirmed", "not_needed"}, "UNCONFIRMED_CLEANUP")
        result = result_map.get(identity)
        if result is not None:
            check(result["call_id"] == execution["call_id"] and result["input_manifest_sha256"] == execution["input_manifest_sha256"],
                  "TEST_RESULT_BINDING_MISMATCH")
            check(all(result[key] == item[key] for key in ("state", "exit_code", "passed", "failed", "public_tests_passed")),
                  "TEST_RESULT_SUMMARY_MISMATCH")
        log_path = f"tests/{item['tool_call_id']}/output.log"
        if (directory / log_path).exists():
            check(absent(read_bytes(directory, log_path).decode("utf-8", errors="replace")), "SECRET_IN_EXECUTION_OUTPUT")
    for artifact in data["artifacts"]:
        content = read_bytes(directory, artifact["relative_path"])
        check(hashlib.sha256(content).hexdigest() == artifact["sha256"] and len(content) == artifact["size_bytes"], "LOG_HASH_MISMATCH")
        execution = execution_map.get(artifact["execution_id"])
        check(execution is not None, "LOG_EXECUTION_MISSING")
        if execution is not None:
            check(execution["call_id"] == artifact["producer_call_id"]
                  and execution["input_manifest_sha256"] == artifact["input_manifest_sha256"]
                  and execution["record"]["artifact_id"] == artifact["artifact_id"], "LOG_EXECUTION_BINDING_MISMATCH")
        check(absent(content.decode("utf-8", errors="replace")), "SECRET_IN_LOG")
    if report.get("last_test_execution_id"):
        last = execution_map.get(report["last_test_execution_id"])
        check(last is not None and last["input_manifest_sha256"] == report["tested_input_manifest_sha256"], "LAST_TEST_BINDING_MISMATCH")
    if report.get("execution_id"):
        execution = execution_map.get(report["execution_id"])
        check(execution is not None and execution["input_manifest_sha256"] == report["input_manifest_sha256"], "GATE_EXECUTION_MISMATCH")
        if execution is not None:
            prefix = f"tests/{execution['call_id']}"
            manifest = json.loads(read_bytes(directory, f"{prefix}/input.manifest.json"))
            check(manifest["sources"] == report["source_hashes"] and manifest["public_tests"] == report["original_test_hashes"],
                  "GATE_SOURCE_TEST_HASH_MISMATCH")
            if report.get("test_record") is not None:
                from repopilot.verification.docker import read_test_record

                record = read_test_record(read_bytes(directory, f"{prefix}/output.log").decode("utf-8"))
                check(record.model_dump(mode="json") == report["test_record"], "GATE_STRUCTURED_LOG_MISMATCH")
    if (directory / "candidate.patch").exists():
        patch = read_bytes(directory, "candidate.patch")
        manifest = json.loads(read_bytes(directory, "candidate.manifest.json"))
        if manifest.get("task_id") is not None:
            check(manifest["task_id"] == report["task_id"], "CANDIDATE_TASK_MISMATCH")
        check(hashlib.sha256(patch).hexdigest() == manifest["patch_sha256"], "CANDIDATE_PATCH_MISMATCH")
        check(absent(patch.decode()), "SECRET_IN_PATCH")
    change_sets = inspect_change_sets(directory, file_events, secret=secret)
    for code in change_sets["issues"]:
        check(False, code)
    check(all(item["complete"] for item in change_sets["change_sets"]), "BATCH_EDIT_INCOMPLETE")
    from repopilot.ledger.checkpoints import inspect_checkpoints
    checkpoints = inspect_checkpoints(directory, file_events, raw, secret=secret)
    for code in checkpoints["issues"]:
        check(False, code)
    if report.get("mode") == "imported_candidate":
        from repopilot.ledger.imports import inspect_import

        imported = inspect_import(directory, file_events, report, run, has_calls=bool(data["calls"]), secret=secret)
        for code in imported["issues"]:
            check(False, code)
    if report.get("mode") == "reconstructed_candidate":
        envelope = ReconstructionEnvelope.model_validate_json(read_bytes(directory, "derivation.json"))
        plan = envelope.plan
        check(digest(plan.model_dump(mode="json")) == envelope.plan_sha256, "DERIVATION_PLAN_HASH_MISMATCH")
        check(run["source_run_id"] == plan.source_run_id and run["task_sha256"] == plan.task_sha256,
              "DERIVATION_SOURCE_MISMATCH")
        bindings = [event for event in file_events if event["event"] == "candidate.reconstruction_intent"]
        completed = [event for event in file_events if event["event"] == "candidate.reconstructed"]
        successful = report["status"] == "script_finished"
        check(len(bindings) == 1 and len(completed) == int(successful), "DERIVATION_EVENT_MISMATCH")
        if len(bindings) == len(completed) == 1:
            check(bindings[0]["plan_sha256"] == completed[0]["plan_sha256"] == envelope.plan_sha256
                  and bindings[0]["source_run_id"] == completed[0]["source_run_id"] == plan.source_run_id,
                  "DERIVATION_BINDING_MISMATCH")
        for name, expected in plan.source_seals.items():
            content = read_bytes(directory, "source-evidence/" + name)
            check(hashlib.sha256(content).hexdigest() == expected, "DERIVATION_SOURCE_HASH_MISMATCH")
            check(absent(content.decode("utf-8", errors="replace")), "SECRET_IN_DERIVATION")
        source_events = [json.loads(line) for line in read_bytes(directory, "source-evidence/events.jsonl").splitlines()]
        with ledger.engine.connect() as connection:
            parent_events = connection.execute(sa.select(events.c.payload).where(events.c.run_id == plan.source_run_id,
                events.c.seq_no <= plan.source_events_count).order_by(events.c.seq_no)).scalars().all()
        check(len(source_events) == plan.source_events_count and source_events == parent_events, "DERIVATION_PARENT_PREFIX_MISMATCH")
        if successful:
            check(json.loads(read_bytes(directory, "candidate.manifest.json")) == plan.candidate.model_dump(mode="json"),
                  "DERIVATION_CANDIDATE_MISMATCH")
        check(model_count == 0 and not data["calls"] and all(value == 0 for value in report["counters"].values())
              and not report["public_tests_passed"] and not report["bug_fixed"], "DERIVATION_NOT_AN_EXECUTION")
    return {"run_id": directory.name, "passed": not issues, "issues": issues,
            "change_sets": change_sets["change_sets"],
            "secret_scan_performed": secret is not None,
            "counts": {name: len(rows) for name, rows in data.items()},
            "model_requests": model_count, "known_tokens": prompt_tokens + completion_tokens,
            "unknown_usage_calls": unknown_usage, "cost_upper_bound_usd": str(model_cost),
            "unresolved_calls": ledger.summary(directory.name)["unresolved_calls"]}
