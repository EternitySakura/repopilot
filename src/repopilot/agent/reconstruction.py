"""Reconstruct a selected source revision into a fresh candidate; never resume calls."""

import hashlib
import json
import traceback
from pathlib import Path, PurePosixPath

from repopilot.agent.loop import LoopOutcome
from repopilot.contracts.action import EDIT_ACTIONS, Action
from repopilot.contracts.candidate import CandidateChange, CandidateManifest
from repopilot.contracts.reconstruction import (
    ReconstructionEnvelope,
    ReconstructionPlan,
)
from repopilot.contracts.run import RunStatus
from repopilot.dispatcher import Dispatcher
from repopilot.ledger.audit import read_bytes
from repopilot.ledger.inspection import inspect_run
from repopilot.ledger.store import Ledger, digest, using_ledger
from repopilot.observability.events import EventLog
from repopilot.observability.reports import RunTracker
from repopilot.repository.tasks import PROJECT_ROOT, fixture_root, load_task
from repopilot.settings import Settings
from repopilot.tools.edit import check_edit_scope
from repopilot.tools.policy import ReadPolicy, ToolFailure
from repopilot.verification.gate_a import rebuild_candidate
from repopilot.workspace import Workspace, render_patch


def file_sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _assemble(ledger: Ledger, source_run_id: str, selected_call_id: str, revision: str):
    if revision not in {"before", "proposed"}:
        raise ValueError("Select the before or proposed source revision")
    inspected = inspect_run(ledger, source_run_id)
    if not inspected["evidence_consistent"]:
        raise ValueError("Source evidence is inconsistent; inspect it before reconstructing")
    summary = ledger.summary(source_run_id)
    task = load_task(summary["task_id"])
    if digest(task.model_dump(mode="json")) != summary["task_sha256"]:
        raise ValueError("Registered task differs from the source run")
    profiles, original = Workspace.read_original(fixture_root(task), task)
    policy = ReadPolicy(fixture_root(task), profile=profiles.repo)
    directory = Path(inspected["artifact_directory"])
    cache = {}

    def read(name):
        if name not in cache:
            cache[name] = read_bytes(directory, name)
        return cache[name]

    raw = read("events.jsonl")
    if file_sha(raw) != inspected["events_sha256"]:
        raise ValueError("Source events changed during reconstruction planning")
    source_profiles = json.loads(read("profiles.json"))
    if source_profiles["profile_sha256"] != profiles.sha256 or digest(source_profiles["profiles"]) != profiles.sha256:
        raise ValueError("Source profiles differ from the registered versions")
    events = [json.loads(line) for line in raw.splitlines()]
    batches = {item["tool_call_id"]: item for item in inspected["change_sets"]}
    if selected_call_id not in batches:
        raise ValueError("Select a recorded edit_files call from this source run")
    state = dict(original)
    selected = False

    def apply_snapshot(items, prefix, *, apply):
        names = set()
        updates = {}
        for item in items:
            name = item["path"]
            if PurePosixPath(name).as_posix() != name or name.casefold() in names:
                raise ToolFailure("INVALID_CANDIDATE_PATH", "Source paths must be canonical and unique")
            names.add(name.casefold())
            check_edit_scope(name, task.editable_globs)
            destination = policy.check(name, additional_public_files=frozenset({name}), allow_missing_leaf=name not in original)
            if name not in original and destination.exists():
                raise ToolFailure("ALREADY_EXISTS", "New source path aliases a baseline file")
            before = state.get(name)
            if (file_sha(before) if before is not None else None) != item["before_sha256"]:
                raise ValueError("Source edit does not follow the confirmed prefix")
            if before is not None and read(prefix(item, "before")) != before:
                raise ValueError("Archived before bytes differ from the confirmed prefix")
            after = read(prefix(item, "after"))
            if file_sha(after) != item["after_sha256"]:
                raise ValueError("Archived after bytes differ from the recorded hash")
            if len(after) > policy.max_bytes or b"\x00" in after:
                raise ToolFailure("NOT_TEXT", "Reconstructed source exceeds its registered text bounds")
            after.decode("utf-8")
            updates[name] = after
        if apply:
            state.update(updates)

    for intent in (event for event in events if event["event"] == "tool.intent" and event.get("action_type") in EDIT_ACTIONS):
        call_id = intent["tool_call_id"]
        terminals = [event for event in events if event["event"] in {"tool.completed", "tool.failed"} and event["tool_call_id"] == call_id]
        terminal = terminals[0] if len(terminals) == 1 else None
        confirmed = terminal is not None and terminal["event"] == "tool.completed" and terminal.get("result", {}).get("ok") is True
        if intent["action_type"] == "edit_files" and call_id in batches:
            batch = batches[call_id]
            journal = json.loads(read(batch["journal_ref"]))
            if file_sha(cache[batch["journal_ref"]]) != batch["journal_sha256"]:
                raise ValueError("Source journal changed during reconstruction planning")
            action = Action.model_validate(intent["action"])
            if [(item["path"], item["before_sha256"]) for item in journal["files"]] != [
                    (item.path, item.expected_sha256) for item in action.arguments.files]:
                raise ValueError("Batch journal differs from its original tool intent")
            is_selected = call_id == selected_call_id
            if not is_selected and not ((confirmed and batch["status"] == "committed") or batch["status"] == "rolled_back"):
                raise ValueError("An earlier edit has an uncertain outcome; select an earlier checkpoint")
            apply = revision == "proposed" if is_selected else confirmed and batch["status"] == "committed"
            apply_snapshot(journal["files"], lambda item, phase, cid=call_id: f"edits/{cid}/{item['index']}/{phase}.bin", apply=apply)
            if is_selected:
                selected = True
                break
        else:
            edits = [event for event in events if event["event"] == "edit.intent" and event["tool_call_id"] == call_id]
            if not confirmed:
                if edits or terminal is None:
                    raise ValueError("An earlier edit has an uncertain outcome; select an earlier checkpoint")
                continue
            if not edits:  # A confirmed no-op has no before/after artifacts.
                continue
            if len(edits) != 1:
                raise ValueError("Source edit has ambiguous artifacts")
            item = edits[0]
            result = terminal["result"]["data"]
            if any(result[key] != item[key] for key in ("path", "before_sha256", "after_sha256")):
                raise ValueError("Edit intent differs from its confirmed result")
            apply_snapshot([item], lambda _item, phase, cid=call_id: f"edits/{cid}/{phase}.bin", apply=True)
    if not selected:
        raise ValueError("Selected change set has no matching intent")
    changes = {name: value for name, value in state.items() if original.get(name) != value}
    patch = render_patch(original, changes)
    candidate = CandidateManifest(task_id=task.task_id, base_revision=task.base_revision, profile_sha256=profiles.sha256,
        patch_sha256=file_sha(patch), changes=[CandidateChange(path=name,
            before_sha256=file_sha(original[name]) if name in original else None,
            after_sha256=file_sha(content), content=content.decode("utf-8")) for name, content in sorted(changes.items())])
    plan = ReconstructionPlan(source_run_id=source_run_id, source_events_count=len(events),
        source_seals={name: file_sha(content) for name, content in sorted(cache.items())},
        source_status=summary["status"], source_unresolved_calls=[item["call_id"] for item in summary["unresolved_calls"]],
        task_id=task.task_id, task_sha256=summary["task_sha256"], profile_sha256=profiles.sha256,
        selected_call_id=selected_call_id, selected_batch_status=batches[selected_call_id]["status"], revision=revision, candidate=candidate)
    return plan, cache, original, task


def freeze_reconstruction(ledger: Ledger, source_run_id: str, selected_call_id: str, revision: str, output: Path):
    plan, _, _, _ = _assemble(ledger, source_run_id, selected_call_id, revision)
    envelope = ReconstructionEnvelope(plan_sha256=digest(plan.model_dump(mode="json")), plan=plan)
    for parent in reversed(output.absolute().parents):
        if parent.exists():
            ReadPolicy._check_node(parent)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(envelope.model_dump_json(indent=2) + "\n")
    return envelope


def load_reconstruction(path: Path):
    envelope = ReconstructionEnvelope.model_validate_json(read_bytes(path.parent, path.name))
    if digest(envelope.plan.model_dump(mode="json")) != envelope.plan_sha256:
        raise ValueError("Reconstruction plan content hash mismatch")
    return envelope


def build_reconstruction(ledger: Ledger, plan_path: Path, runs: Path | None = None):
    envelope = load_reconstruction(plan_path)
    plan = envelope.plan
    current, cache, original, task = _assemble(ledger, plan.source_run_id, plan.selected_call_id, plan.revision)
    if current != plan:
        raise ValueError("Source or registered inputs changed; create a new reconstruction plan")
    config = Settings()
    with using_ledger(ledger):
        events = EventLog(runs if runs is not None else PROJECT_ROOT / "artifacts/runs")
        events.register_task(task)
        tracker = RunTracker(events, config, mode="reconstructed_candidate", task_id=task.task_id, source_run_id=plan.source_run_id)
        try:
            (events.directory / "derivation.json").write_text(envelope.model_dump_json(indent=2) + "\n", encoding="utf-8")
            for name, content in cache.items():
                target = events.directory / "source-evidence" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            events.write("candidate.reconstruction_intent", plan_sha256=envelope.plan_sha256,
                         source_run_id=plan.source_run_id, selected_call_id=plan.selected_call_id, revision=plan.revision)
            workspace = Workspace(fixture_root(task), events.directory / "workspace", task)
            (events.directory / "task.json").write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")
            workspace.profiles.save_snapshot(events.directory, settings=config)
            Dispatcher(workspace.policy, events)  # Read-only capability snapshot; no tool calls are issued.
            changes = {item.path: item.content.encode("utf-8") for item in plan.candidate.changes}
            patch = render_patch(original, changes)
            if changes:
                rebuild_candidate(workspace, task, plan.candidate, patch, events)
            workspace.export_diff(events.directory / "candidate.patch")
            if json.loads(read_bytes(events.directory, "candidate.manifest.json")) != plan.candidate.model_dump(mode="json"):
                raise RuntimeError("Reconstructed candidate differs from its frozen plan")
            events.write("candidate.reconstructed", plan_sha256=envelope.plan_sha256,
                         source_run_id=plan.source_run_id, patch_sha256=plan.candidate.patch_sha256)
            tracker.finish(LoopOutcome(RunStatus.SCRIPT_FINISHED, 0, (), "candidate_reconstructed"),
                           generation=int(bool(changes)), patch_ref=str(events.directory / "candidate.patch"))
        except Exception as exc:
            tracker.finish(None, diagnostic=traceback.format_exc(), failure_reason="reconstruction_error")
            exc.reconstruction_ref = str(events.directory / "report.md")
            raise
    return events
