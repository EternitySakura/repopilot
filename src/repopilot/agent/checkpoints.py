"""Clean-boundary pause/resume, with ledger-bound state and cumulative budgets."""

import json
from decimal import Decimal
from pathlib import Path
from time import monotonic
from uuid import uuid4

import sqlalchemy as sa
from pydantic import SecretStr

from repopilot.agent.decision_json import PARSER_VERSION
from repopilot.agent.deepseek import PROMPT_VERSION, DeepSeekModel
from repopilot.agent.loop import LoopState
from repopilot.contracts.candidate import CandidateManifest
from repopilot.contracts.checkpoint import (
    Checkpoint,
    CheckpointEnvelope,
    LoopCheckpoint,
)
from repopilot.contracts.run import RunReport
from repopilot.dispatcher import Dispatcher
from repopilot.ledger.audit import audit_run, read_bytes
from repopilot.ledger.inspection import inspect_run
from repopilot.ledger.schema_v1 import artifacts, executions
from repopilot.ledger.schema_v1 import events as event_table
from repopilot.ledger.store import Ledger, digest, using_ledger
from repopilot.observability.events import EventLog
from repopilot.observability.reports import RunTracker
from repopilot.repository.profiles import resolve_profiles
from repopilot.repository.tasks import fixture_root, load_public_task, load_task
from repopilot.settings import LLMSettings, Settings
from repopilot.tools.edit import Editor
from repopilot.tools.registry import action_schema, enabled_tools
from repopilot.tools.testing import DockerTests
from repopilot.verification.gate_a import rebuild_candidate
from repopilot.workspace import Workspace, render_patch


def code_sha():
    from repopilot.agent.live_acceptance import code_snapshot
    return code_snapshot()["source_tree_sha256"]


def save_checkpoint(events: EventLog, tracker: RunTracker, model: DeepSeekModel, editor: Editor):
    if events.ledger is None:
        raise ValueError("Durable pause requires a controller ledger")
    report = RunReport.model_validate_json(read_bytes(events.directory, "run.json"))
    accounting = model.accounting()
    if (report.status != "paused" or accounting.unknown_usage_calls
            or len(model.assistant_decisions) != len(tracker.state.observations)):
        raise ValueError("Only a clean completed-tool boundary can checkpoint")
    checked = audit_run(events.ledger, events.directory, secret=model.config.api_key.get_secret_value())
    if not checked["passed"] or checked["unresolved_calls"]:
        raise ValueError("Paused evidence must reconcile before it grants resume authority")
    snapshot = events.directory / "checkpoints" / uuid4().hex
    snapshot.mkdir(parents=True)
    seals = {}
    for name in ("run.json", "report.md", "task.json", "profiles.json", "tools.json", "artifacts.json", "executions.json",
                 "candidate.patch", "candidate.manifest.json"):
        if (events.directory / name).exists():
            raw = read_bytes(events.directory, name)
            (snapshot / name).write_bytes(raw)
            seals[name] = digest_bytes(raw)
    task = load_task(report.task_id)
    state = tracker.state
    checkpoint = Checkpoint(run_id=events.run_id, task_id=task.task_id, task_sha256=digest(task.model_dump(mode="json")),
        profile_sha256=resolve_profiles(task).sha256, code_sha256=code_sha(), prompt_version=PROMPT_VERSION,
        parser_version=PARSER_VERSION, enabled_tools=list(model.enabled_tools), schema_sha256=model.schema_sha256,
        settings=tracker.settings.model_dump(mode="json"), llm=model.config.model_dump(mode="json"), accounting=accounting,
        assistant_decisions=model.assistant_decisions, generation=editor.generation, events_count=events.sequence,
        events_sha256=digest_bytes(read_bytes(events.directory, "events.jsonl")), seals=seals,
        candidate=CandidateManifest.model_validate_json(read_bytes(snapshot, "candidate.manifest.json")),
        loop=LoopCheckpoint(counters=report.counters, observations=state.observations, evidence=sorted(state.evidence),
            failure_fingerprints=state.failure_fingerprints, last_test=state.last_test))
    envelope = CheckpointEnvelope(checkpoint_sha256=digest(checkpoint.model_dump(mode="json")), checkpoint=checkpoint)
    text = envelope.model_dump_json(indent=2) + "\n"
    if model.config.api_key.get_secret_value() in text:
        raise RuntimeError("Refusing to persist a credential in a checkpoint")
    (snapshot / "checkpoint.json").write_text(text, encoding="utf-8")
    events.write("run.checkpointed", checkpoint_sha256=envelope.checkpoint_sha256,
                 checkpoint_ref=(snapshot / "checkpoint.json").relative_to(events.directory).as_posix())
    return snapshot


def digest_bytes(raw):
    from hashlib import sha256
    return sha256(raw).hexdigest()


def verified_checkpoint(ledger: Ledger, run_id: str):
    info = ledger.summary(run_id)
    if info["status"] != "paused" or info["mode"] != "live_candidate" or info["unresolved_calls"]:
        raise ValueError("Select a clean paused live run; unfinished or terminal runs cannot resume")
    directory = Path(info["artifact_directory"])
    with ledger.engine.connect() as connection:
        rows = connection.execute(sa.select(event_table.c.payload).where(event_table.c.run_id == run_id)
                                  .order_by(event_table.c.seq_no)).scalars().all()
        artifact_records = connection.execute(sa.select(artifacts.c.record).where(artifacts.c.run_id == run_id)).scalars().all()
        execution_records = connection.execute(sa.select(executions.c.record).where(executions.c.run_id == run_id)).scalars().all()
    bound = [row for row in rows if row["event"] == "run.checkpointed"]
    if not bound:
        raise ValueError("Paused run has no ledger-bound checkpoint")
    reference = bound[-1]["checkpoint_ref"]
    envelope = CheckpointEnvelope.model_validate_json(read_bytes(directory, reference))
    data = envelope.checkpoint
    if (digest(data.model_dump(mode="json")) != envelope.checkpoint_sha256
            or envelope.checkpoint_sha256 != bound[-1]["checkpoint_sha256"] or data.run_id != run_id):
        raise ValueError("Checkpoint binding or content hash differs")
    snapshot = directory / Path(reference).parent
    for name, sha in data.seals.items():
        if digest_bytes(read_bytes(snapshot, name)) != sha:
            raise ValueError("Checkpoint snapshot changed")
    raw_events = read_bytes(directory, "events.jsonl").splitlines(keepends=True)
    if digest_bytes(b"".join(raw_events[:data.events_count])) != data.events_sha256:
        raise ValueError("Checkpoint event prefix changed")
    task = load_public_task(data.task_id)
    config = Settings(**data.settings)
    llm = LLMSettings(_env_file=None, api_key=SecretStr("checkpoint-verification-not-a-real-key"), **data.llm)
    names = enabled_tools(can_edit=True, can_test=True, can_diff=True)
    from hashlib import sha256
    schema_sha = sha256(json.dumps(action_schema(names), sort_keys=True).encode()).hexdigest()
    if (data.code_sha256 != code_sha() or data.prompt_version != PROMPT_VERSION or data.parser_version != PARSER_VERSION
            or data.schema_sha256 != schema_sha or data.enabled_tools != list(names)
            or data.task_sha256 != info["task_sha256"] or digest(task.model_dump(mode="json")) != data.task_sha256
            or resolve_profiles(task).sha256 != data.profile_sha256 or config.model_dump(mode="json") != data.settings
            or llm.model_dump(mode="json") != data.llm):
        raise ValueError("Controller, task, profile or frozen configuration changed")
    report = RunReport.model_validate_json(read_bytes(snapshot, "run.json"))
    if json.loads(read_bytes(directory, "run.json")) != report.model_dump(mode="json"):
        raise ValueError("Latest paused report differs from its bound checkpoint")
    if (report.status != "paused" or report.accounting.model_dump(mode="json") != data.accounting.model_dump(mode="json")
            or report.counters != data.loop.counters or data.accounting.unknown_usage_calls
            or data.accounting.requests != data.loop.counters.decisions
            or len(data.assistant_decisions) != len(data.loop.observations)
            or len(data.loop.observations) != data.loop.counters.tool_calls):
        raise ValueError("Checkpoint controller/model accounting is inconsistent")
    checked = audit_run(ledger, directory)
    if not checked["passed"] or checked["unresolved_calls"]:
        raise ValueError("Paused run failed ledger reconciliation")
    for filename, records, key in (("artifacts.json", artifact_records, "artifact_id"),
                                  ("executions.json", execution_records, "execution_id")):
        saved = json.loads(read_bytes(snapshot, filename))["records"] if filename in data.seals else []
        if sorted(saved, key=lambda row: row[key]) != sorted(records, key=lambda row: row[key]):
            raise ValueError("Persisted capabilities differ from the ledger")
    if any(row["state"] != "completed" or row["cleanup_status"] != "confirmed" for row in execution_records):
        raise ValueError("Unconfirmed executions cannot resume")
    return envelope, snapshot, report, artifact_records, execution_records


def resume_preview(ledger: Ledger, run_id: str):
    envelope, _, report, _, _ = verified_checkpoint(ledger, run_id)
    data = envelope.checkpoint
    info = ledger.summary(run_id)
    log = EventLog.attach_paused(ledger, run_id, Path(info["artifact_directory"]), info["counts"]["events"], report.evaluation_context)
    llm = LLMSettings(_env_file=None, api_key=SecretStr("preview-only-not-a-real-key"), **data.llm)
    model = _restore_model(data, log, llm, Settings(**data.settings))
    return {"run_id": run_id, "checkpoint_sha256": envelope.checkpoint_sha256, "task_id": data.task_id,
        "endpoint": data.llm["base_url"], "model": data.llm["model_id"], "requests_used": data.accounting.requests,
        **checkpoint_budget(data, report), "preview_model_requests": 0,
        "messages": model.messages(data.loop.observations)}


def checkpoint_budget(data, report):
    """Cumulative limits shared by outbound preview and read-only run inspection."""
    return {
        "remaining_model_calls": data.llm["max_calls"] - data.accounting.requests,
        "remaining_decisions": data.settings["max_iterations"] - data.loop.counters.decisions,
        "remaining_tool_calls": data.settings["max_tool_calls"] - data.loop.counters.tool_calls,
        "remaining_edit_calls": data.settings["max_edit_calls"] - data.loop.counters.edit_calls,
        "remaining_test_calls": data.settings["max_test_calls"] - data.loop.counters.test_calls,
        "cost_upper_bound_usd": data.accounting.cost_upper_bound_usd, "max_cost_usd": data.accounting.max_cost_usd,
        "remaining_cost_cap_usd": str(Decimal(data.accounting.max_cost_usd) - Decimal(data.accounting.cost_upper_bound_usd)),
        "remaining_active_seconds": (max(0, data.settings["max_run_seconds"] - report.duration_ms / 1000)
                                     if data.settings["max_run_seconds"] is not None else None),
        "active_elapsed_seconds": report.duration_ms / 1000}


def _restore_model(data, log, llm, config, transport=None):
    model = DeepSeekModel(llm, load_task(data.task_id).agent_view(), log, transport=transport)
    model.configure_tools(tuple(data.enabled_tools))
    model.configure_context(resolve_profiles(load_task(data.task_id)).repo.public_files, config)
    accounting = data.accounting
    model.requests = accounting.requests
    model.prompt_tokens, model.completion_tokens = accounting.known_prompt_tokens, accounting.known_completion_tokens
    model.cache_hits, model.unknown_calls = accounting.known_cache_hit_tokens, accounting.unknown_usage_calls
    model.spent_bound, model.response_models = Decimal(accounting.cost_upper_bound_usd), set(accounting.response_models)
    model.assistant_decisions = data.assistant_decisions
    return model


def resume_live(ledger: Ledger, run_id: str, *, llm_settings=None, transport=None, pause_after_tools=None, verifications=None):
    envelope, _snapshot, report, artifact_records, execution_records = verified_checkpoint(ledger, run_id)
    data = envelope.checkpoint
    if pause_after_tools is not None and pause_after_tools <= data.loop.counters.tool_calls:
        raise ValueError("Next pause must follow a later cumulative tool call")
    if data.accounting.requests >= data.llm["max_calls"] or data.loop.counters.decisions >= data.settings["max_iterations"]:
        raise ValueError("No model decision budget remains")
    current_key = (llm_settings if llm_settings is not None else LLMSettings()).api_key
    llm = LLMSettings(_env_file=None, api_key=current_key, **data.llm)
    task = load_task(data.task_id)
    config = Settings(**data.settings)
    from repopilot.agent.live import check_environment
    check_environment(resolve_profiles(task), config)
    inspected = inspect_run(ledger, run_id)
    directory = Path(inspected["artifact_directory"])
    sequence = len(read_bytes(directory, "events.jsonl").splitlines())
    log = EventLog.attach_paused(ledger, run_id, directory, sequence, report.evaluation_context)
    resume_id = uuid4().hex
    log.write("run.resumed", checkpoint_sha256=envelope.checkpoint_sha256, resume_id=resume_id)
    elapsed = report.duration_ms / 1000
    loop = data.loop
    state = LoopState(started=monotonic() - elapsed, **loop.counters.model_dump(), observations=loop.observations,
        evidence=set(loop.evidence), failure_fingerprints=loop.failure_fingerprints, last_test=loop.last_test)
    model = _restore_model(data, log, llm, config, transport)
    tracker = RunTracker(log, config, mode="live_candidate", task_id=task.task_id, accounting=model.accounting(),
        state=state, started_at=report.started_at, elapsed_seconds=elapsed, resuming=True)
    workspace = None
    editor = None
    try:
        # Every paused report remains sealed in its checkpoint folder before these active views change.
        for filename in ("run.json", "report.md"):
            (directory / filename).unlink()
        workspace = Workspace(fixture_root(task), directory / "segments" / resume_id / "workspace", task)
        if data.candidate.changes:
            changes = {item.path: item.content.encode("utf-8") for item in data.candidate.changes}
            rebuild_candidate(workspace, task, data.candidate, render_patch(workspace.original, changes), log)
        editor = Editor(workspace.policy, task.editable_globs, log)
        editor.generation = data.generation
        runner = DockerTests(workspace, directory, timeout=config.tool_timeout_seconds)
        dispatcher = Dispatcher(workspace.policy, log, editor=editor, tests=runner, workspace=workspace)
        runner.executions.restore_verified(execution_records)
        dispatcher.artifacts.restore_verified(artifact_records)
    except Exception:
        from repopilot.agent.live import _fail_live
        _fail_live(log, tracker, model, workspace, editor)
        raise
    from repopilot.agent.live import _drive_live
    with using_ledger(ledger):
        return _drive_live(model, log, tracker, workspace, editor, dispatcher, config,
                           pause_after_tools=pause_after_tools, verifications=verifications)
