"""Register a public bundle as a new local candidate under controller-owned inputs."""

import json
import traceback
from dataclasses import dataclass
from pathlib import Path

from repopilot.agent.loop import LoopOutcome
from repopilot.contracts.candidate import CandidateManifest
from repopilot.contracts.run import RunStatus
from repopilot.contracts.task import AgentTaskView, TaskSpec
from repopilot.dispatcher import Dispatcher
from repopilot.ledger.audit import read_bytes
from repopilot.ledger.store import Ledger, digest, using_ledger
from repopilot.observability.events import EventLog
from repopilot.observability.reports import RunTracker
from repopilot.repository.profiles import resolve_profiles
from repopilot.repository.tasks import (
    PROJECT_ROOT,
    RUNTIME_V2_TASKS,
    fixture_root,
    load_task,
)
from repopilot.settings import Settings
from repopilot.tools.errors import ToolFailure
from repopilot.verification.bundle import (
    PublicVerification,
    file_sha,
    match_bundle_source,
    read_checked_bundle,
)
from repopilot.verification.gate_a import rebuild_candidate
from repopilot.workspace import Workspace


@dataclass(frozen=True)
class PreparedImport:
    task: TaskSpec
    candidate: CandidateManifest
    files: dict[str, bytes]
    metadata: dict
    output: Path


def bind_registered_candidate(data: dict[str, bytes], *, runtime_v2: bool = False) -> tuple[TaskSpec, CandidateManifest, dict | None]:
    """Bind a checked bundle to its registered task or its explicit v2 successor."""
    public = AgentTaskView.model_validate_json(data["task.json"])
    task = load_task(public.task_id)
    profiles, original = Workspace.read_original(fixture_root(task), task)
    baseline = {name.removeprefix("baseline/"): value for name, value in data.items() if name.startswith("baseline/")}
    gate = PublicVerification.model_validate_json(data["verification.json"])
    candidate = CandidateManifest.model_validate_json(data["candidate.manifest.json"])
    if (public != task.agent_view() or baseline != original or candidate.profile_sha256 != profiles.sha256
            or gate.expected_tests != list(profiles.test.expected_nodes) or gate.environment_digest != profiles.repo.image_id):
        raise ValueError("Bundle differs from the registered public task, baseline or environment")
    if not runtime_v2:
        return task, candidate, None

    target_id = next((identity for identity, source in RUNTIME_V2_TASKS.items() if source == task.task_id), None)
    if target_id is None:
        raise ToolFailure("RUNTIME_MIGRATION_UNAVAILABLE", "Only a registered public v1 development bundle can migrate to v2")
    target = load_task(target_id)
    target_profiles = resolve_profiles(target)
    expected_repo = profiles.repo.model_copy(update={"version": 2, "image_id": target_profiles.repo.image_id})
    if target_profiles != profiles.model_copy(update={"repo": expected_repo}):
        raise ToolFailure("RUNTIME_MIGRATION_INCOMPATIBLE", "The v2 registration differs beyond its runtime binding")
    migrated = candidate.model_copy(update={"task_id": target_id, "profile_sha256": target_profiles.sha256})
    migration = {
        "source_task_id": task.task_id, "target_task_id": target_id,
        "source_profile_sha256": profiles.sha256, "target_profile_sha256": target_profiles.sha256,
        "source_environment_digest": gate.environment_digest, "target_environment_digest": target_profiles.repo.image_id,
    }
    return target, migrated, migration


def prepare_bundle_import(directory: Path, runs: Path | None = None, *,
                          source_ledger: Ledger | None = None, runtime_v2: bool = False) -> PreparedImport:
    checked, data = read_checked_bundle(directory)
    task, candidate, migration = bind_registered_candidate(data, runtime_v2=runtime_v2)
    output = (runs if runs is not None else PROJECT_ROOT / "artifacts/runs").resolve()
    if output.is_relative_to(directory.resolve()):
        raise ValueError("Imported runs must stay outside the preserved input bundle")
    metadata = {"version": 1, "bundle_sha256": checked["bundle_sha256"],
                "external_source_run_id": checked["source_run_id"],
                "external_verification_id": checked["verification_id"],
                "task_sha256": digest(task.model_dump(mode="json")),
                "candidate_sha256": file_sha(data["candidate.manifest.json"])}
    if source_ledger is not None:
        matched = match_bundle_source(source_ledger, checked, data)
        metadata["source_check_at_import"] = matched["ledger_verification_scope"]
    if migration is not None:
        metadata["runtime_migration"] = migration
    return PreparedImport(task, candidate, data, metadata, output)


def import_bundle(ledger: Ledger, directory: Path, runs: Path | None = None, *, runtime_v2: bool = False) -> EventLog:
    return register_bundle_import(ledger, prepare_bundle_import(directory, runs, runtime_v2=runtime_v2))


def register_bundle_import(ledger: Ledger, prepared: PreparedImport) -> EventLog:
    task, metadata = prepared.task, prepared.metadata
    config = Settings()
    with using_ledger(ledger):
        events = EventLog(prepared.output)
        events.register_task(task)
        tracker = RunTracker(events, config, mode="imported_candidate", task_id=task.task_id)
        try:
            (events.directory / "import.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            events.write("candidate.import_intent", **metadata)
            snapshot = events.directory / "bundle"
            snapshot.mkdir()
            for name, content in prepared.files.items():
                path = snapshot / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            workspace = Workspace(fixture_root(task), events.directory / "workspace", task)
            (events.directory / "task.json").write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")
            workspace.profiles.save_snapshot(events.directory, settings=config)
            Dispatcher(workspace.policy, events)
            rebuild_candidate(workspace, task, prepared.candidate, prepared.files["candidate.patch"], events)
            workspace.export_diff(events.directory / "candidate.patch")
            exported = CandidateManifest.model_validate_json(read_bytes(events.directory, "candidate.manifest.json"))
            if exported != prepared.candidate:
                raise RuntimeError("Rebuilt candidate differs from the prepared manifest")
            events.write("candidate.imported", **metadata)
            tracker.finish(LoopOutcome(RunStatus.SCRIPT_FINISHED, 0, (), "candidate_imported"),
                           generation=1, patch_ref=str(events.directory / "candidate.patch"))
        except Exception as exc:
            tracker.finish(None, diagnostic=traceback.format_exc(), failure_reason="import_error")
            exc.import_ref = str(events.directory / "report.md")
            raise
    return events
