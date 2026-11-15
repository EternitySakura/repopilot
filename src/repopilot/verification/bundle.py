"""Export public candidates and check their contents or ledger provenance."""

import hashlib
import json
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from pydantic import Field

from repopilot.contracts.base import Contract
from repopilot.contracts.candidate import CandidateManifest
from repopilot.contracts.task import AgentTaskView, TaskSpec
from repopilot.contracts.verification import TestRecord, VerificationReport
from repopilot.ledger.audit import audit_run, read_bytes
from repopilot.ledger.schema_v1 import runs, tasks
from repopilot.ledger.store import digest
from repopilot.repository.tasks import HOLDOUT_TASK_IDS, fixture_root, load_task
from repopilot.tools.edit import check_edit_scope
from repopilot.tools.errors import ToolFailure
from repopilot.tools.policy import ReadPolicy
from repopilot.verification.bundle_archive import encode_archive, read_archive
from repopilot.verification.gate_a import check_test_record
from repopilot.workspace import Workspace, render_patch


class PublicVerification(Contract):
    source_run_id: str
    verification_id: str
    task_id: str
    patch_sha256: str
    manifest_sha256: str
    profile_sha256: str
    environment_digest: str
    expected_tests: list[str]
    test_record: TestRecord
    scope: Literal["public_gate_a"] = "public_gate_a"
    final_gate_b: Literal[False] = False


class BundleManifest(Contract):
    version: Literal[1] = 1
    source_run_id: str
    verification_id: str
    files: dict[str, str] = Field(min_length=1, max_length=200)
    verification_history: list[dict]


class BundleEnvelope(Contract):
    sha256: str
    manifest: BundleManifest


def file_sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _check_output(destination: Path, protected):
    for node in (*reversed(destination.absolute().parents), destination.absolute()):
        if node.exists():
            ReadPolicy._check_node(node)
    if any(destination.resolve().is_relative_to(root.resolve()) for root in protected):
        raise ValueError("Output must stay outside preserved input evidence")


def _new_output(destination: Path, protected):
    _check_output(destination, protected)
    destination.mkdir(parents=True, exist_ok=False)


def check_bundle(directory: Path) -> dict:
    """Integrity and scope checks, not proof of authorship or a new test run."""
    result, _ = read_checked_bundle(directory)
    return result


def read_checked_bundle(directory: Path) -> tuple[dict, dict[str, bytes]]:
    """Return the summary and the exact bytes covered by the integrity check."""
    archive = read_archive(directory) if directory.suffix.lower() == ".zip" and not directory.is_dir() else None
    envelope_raw = archive.pop("bundle.json", None) if archive is not None else read_bytes(directory, "bundle.json")
    if envelope_raw is None:
        raise ValueError("Bundle ZIP is missing bundle.json")
    envelope = BundleEnvelope.model_validate_json(envelope_raw)
    manifest = envelope.manifest
    if digest(manifest.model_dump(mode="json")) != envelope.sha256:
        raise ValueError("Bundle manifest hash mismatch")
    data = archive if archive is not None else {name: read_bytes(directory, name) for name in manifest.files}
    if data.keys() != manifest.files.keys() or any(file_sha(data[name]) != sha for name, sha in manifest.files.items()):
        raise ValueError("Bundle file hash mismatch")
    expected = {"task.json", "candidate.patch", "candidate.manifest.json", "verification.json"}
    if not expected.issubset(data):
        raise ValueError("Bundle is missing required entries")
    task = AgentTaskView.model_validate_json(data["task.json"])
    candidate = CandidateManifest.model_validate_json(data["candidate.manifest.json"])
    gate = PublicVerification.model_validate_json(data["verification.json"])
    original = {name.removeprefix("baseline/"): content for name, content in data.items() if name.startswith("baseline/")}
    baseline_sha = file_sha(json.dumps({name: file_sha(content) for name, content in original.items()}, sort_keys=True).encode())
    if (baseline_sha != task.base_revision or candidate.base_revision != task.base_revision
            or candidate.task_id not in {None, task.task_id} or gate.task_id != task.task_id
            or gate.profile_sha256 != candidate.profile_sha256
            or gate.source_run_id != manifest.source_run_id or gate.verification_id != manifest.verification_id
            or gate.patch_sha256 != candidate.patch_sha256 or file_sha(data["candidate.patch"]) != candidate.patch_sha256
            or file_sha(data["candidate.manifest.json"]) != gate.manifest_sha256):
        raise ValueError("Bundle candidate, task and verification binding differs")
    changed = {}
    for change in candidate.changes:
        check_edit_scope(change.path, task.editable_globs)
        content = change.content.encode("utf-8")
        old = original.get(change.path)
        if (change.path in changed or (file_sha(old) if old is not None else None) != change.before_sha256
                or file_sha(content) != change.after_sha256 or data.get("changes/" + change.path) != content):
            raise ValueError("Bundle source content differs")
        changed[change.path] = content
    if not changed or render_patch(original, changed) != data["candidate.patch"]:
        raise ValueError("Bundle patch is not canonical")
    if check_test_record(gate.test_record, gate.test_record.exit_code, expected_tests=gate.expected_tests)[0] != "passed":
        raise ValueError("Bundle does not contain a passing public test record")
    expected |= {"baseline/" + name for name in original} | {"changes/" + name for name in changed}
    if set(data) != expected:
        raise ValueError("Bundle contains unrecognized entries")
    if archive is None:
        actual = set()
        for path in directory.rglob("*"):
            ReadPolicy._check_node(path)
            if path.is_file():
                actual.add(path.relative_to(directory).as_posix())
        if actual != expected | {"bundle.json"}:
            raise ValueError("Bundle has missing or additional files")
    selected = [row for row in manifest.verification_history if row.get("verification_id") == gate.verification_id]
    if len(selected) != 1 or selected[0].get("status") != "passed":
        raise ValueError("Selected verification differs from exported history")
    return {"valid": True, "source_run_id": manifest.source_run_id, "verification_id": gate.verification_id,
            "task_id": task.task_id, "changed_files": sorted(changed), "bundle_sha256": envelope.sha256,
            "verification_history": manifest.verification_history, "new_test_runs": 0, "model_requests": 0,
            "authenticity_verified": False, "final_gate_b": False}, {**data, "bundle.json": envelope_raw}


def pack_bundle(directory: Path, destination: Path) -> dict:
    if not directory.is_dir() or destination.suffix.lower() != ".zip":
        raise ValueError("Pack requires a bundle directory and a new .zip output file")
    result, files = read_checked_bundle(directory)
    data = encode_archive(files)
    _check_output(destination, (directory,))
    destination.parent.mkdir(parents=True, exist_ok=True)
    stream = destination.open("xb")
    try:
        with stream:
            stream.write(data)
    except (OSError, KeyboardInterrupt):
        destination.unlink(missing_ok=True)
        raise
    return {**result, "archive_sha256": file_sha(data)}


def audited_public_gate(ledger, run_id: str, gate_id: str) -> tuple[dict, Path, Path, VerificationReport]:
    source, verification = ledger.summary(run_id), ledger.summary(gate_id)
    if source["status"] in {"running", "paused"} or source["unresolved_calls"]:
        raise ValueError("Export requires a stopped source with resolved calls")
    if verification["mode"] != "cleanroom_public_gate_a" or verification["status"] != "passed":
        raise ValueError("Select a passing independent public Gate A")
    source_dir, gate_dir = Path(source["artifact_directory"]), Path(verification["artifact_directory"])
    if source_dir.name != run_id or gate_dir.name != gate_id:
        raise ValueError("Recorded artifact directory does not match its run ID")
    for directory in (source_dir, gate_dir):
        try:
            checked = audit_run(ledger, directory)
        except (OSError, ValueError, KeyError, TypeError, ToolFailure) as exc:
            raise ValueError("Candidate or verification evidence could not be read") from exc
        if not checked["passed"] or checked["unresolved_calls"]:
            raise ValueError("Candidate or verification evidence failed ledger reconciliation")
    gate = VerificationReport.model_validate_json(read_bytes(gate_dir, "verification.json"))
    if (gate.source_run_id != run_id or gate.verification_id != gate_id or gate.task_id != source["task_id"]
            or gate.status != "passed" or not gate.cleanroom_verified
            or verification["task_sha256"] != source["task_sha256"]):
        raise ValueError("Selected Gate A does not verify this frozen candidate")
    return source, source_dir, gate_dir, gate


def _public_verification(gate: VerificationReport) -> PublicVerification:
    return PublicVerification(source_run_id=gate.source_run_id, verification_id=gate.verification_id, task_id=gate.task_id,
        patch_sha256=gate.patch_sha256, manifest_sha256=gate.manifest_sha256, profile_sha256=gate.profile_sha256,
        environment_digest=gate.environment_digest, expected_tests=gate.verification_scope.expected_tests, test_record=gate.test_record)


def check_bundle_source(ledger, directory: Path) -> dict:
    """Match the checked bundle and selected Gate against the operator's ledger."""
    result, data = read_checked_bundle(directory)
    return match_bundle_source(ledger, result, data)


def match_bundle_source(ledger, result: dict, data: dict[str, bytes]) -> dict:
    """Check the already validated bytes so callers can use the same snapshot."""
    source, source_dir, _, gate = audited_public_gate(ledger, result["source_run_id"], result["verification_id"])
    with ledger.engine.connect() as connection:
        spec = connection.execute(sa.select(tasks.c.spec).where(
            tasks.c.spec_sha256 == source["task_sha256"])).scalar_one_or_none()
    if spec is None or digest(spec) != source["task_sha256"]:
        raise ToolFailure("BUNDLE_SOURCE_TASK_INVALID", "Stored source task does not match its recorded version")
    task = TaskSpec.model_validate(spec)
    if AgentTaskView.model_validate_json(data["task.json"]) != task.agent_view():
        raise ToolFailure("BUNDLE_SOURCE_TASK_MISMATCH", "Bundle task differs from the source task")
    if any(data[name] != read_bytes(source_dir, name) for name in ("candidate.patch", "candidate.manifest.json")):
        raise ToolFailure("BUNDLE_SOURCE_CANDIDATE_MISMATCH", "Bundle candidate differs from the saved source")
    selected = next(row for row in result["verification_history"] if row.get("verification_id") == gate.verification_id)
    if (PublicVerification.model_validate_json(data["verification.json"]) != _public_verification(gate)
            or selected.get("patch_sha256") != gate.patch_sha256):
        raise ToolFailure("BUNDLE_SOURCE_GATE_MISMATCH", "Bundle verification differs from the selected ledger Gate A")
    return {**result, "ledger_source_verified": True, "ledger_verification_scope": "selected_public_gate_a"}


def export_bundle(ledger, run_id: str, gate_id: str, destination: Path) -> dict:
    source, source_dir, gate_dir, gate = audited_public_gate(ledger, run_id, gate_id)
    if source["task_id"] in HOLDOUT_TASK_IDS:
        raise ToolFailure("HOLDOUT_EXPORT_DENIED", "Holdout candidates are controller-only during qualification")
    raw = read_bytes(source_dir, "candidate.manifest.json")
    candidate = CandidateManifest.model_validate_json(raw)
    task = load_task(source["task_id"])
    profiles, original = Workspace.read_original(fixture_root(task), task)
    if (gate.manifest_sha256 != file_sha(raw)
            or candidate.profile_sha256 != profiles.sha256 or gate.profile_sha256 != profiles.sha256
            or source["task_sha256"] != digest(task.model_dump(mode="json"))):
        raise ValueError("Selected Gate A does not verify this frozen candidate")
    with ledger.engine.connect() as connection:
        records = connection.execute(sa.select(runs.c.run_id, runs.c.status, runs.c.report).where(
            runs.c.source_run_id == run_id, runs.c.mode == "cleanroom_public_gate_a").order_by(runs.c.created_at, runs.c.run_id)).mappings().all()
    history = [{"verification_id": row["run_id"], "status": row["status"],
                "patch_sha256": (row["report"] or {}).get("patch_sha256")} for row in records]
    public_gate = _public_verification(gate)
    data = {"task.json": _json(task.agent_view().model_dump(mode="json")), "candidate.manifest.json": raw,
            "candidate.patch": read_bytes(source_dir, "candidate.patch"), "verification.json": public_gate.model_dump_json(indent=2).encode()}
    data.update({"baseline/" + name: content for name, content in original.items()})
    data.update({"changes/" + change.path: change.content.encode("utf-8") for change in candidate.changes})
    manifest = BundleManifest(source_run_id=run_id, verification_id=gate_id, files={name: file_sha(content) for name, content in data.items()},
                              verification_history=history)
    envelope = BundleEnvelope(sha256=digest(manifest.model_dump(mode="json")), manifest=manifest)
    _new_output(destination, (source_dir, gate_dir))
    for name, content in data.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (destination / "bundle.json").write_text(envelope.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return check_bundle(destination)


def materialize_bundle(directory: Path, destination: Path) -> dict:
    """Create a reviewable public working copy; never overwrite another repository."""
    result, data = read_checked_bundle(directory)
    files = {name.removeprefix("baseline/"): content for name, content in data.items() if name.startswith("baseline/")}
    files.update({name.removeprefix("changes/"): content for name, content in data.items() if name.startswith("changes/")})
    _new_output(destination, (directory,))
    for name, content in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return {**result, "workspace": str(destination.absolute()), "materialized_files": len(files),
            "public_tests_executed": False}


def compare_bundles(left: Path, right: Path) -> dict:
    """Compare the final source trees of two checked bundles on the same baseline."""
    left_info, left_data = read_checked_bundle(left)
    right_info, right_data = read_checked_bundle(right)
    left_task = AgentTaskView.model_validate_json(left_data["task.json"])
    right_task = AgentTaskView.model_validate_json(right_data["task.json"])
    left_candidate = CandidateManifest.model_validate_json(left_data["candidate.manifest.json"])
    right_candidate = CandidateManifest.model_validate_json(right_data["candidate.manifest.json"])
    if left_task != right_task or left_candidate.profile_sha256 != right_candidate.profile_sha256:
        raise ToolFailure("BUNDLE_COMPARISON_SCOPE_MISMATCH", "Compare bundles from the same task, baseline and profile")

    left_sources = {}
    updates = {}
    files = []
    for path in sorted(set(left_info["changed_files"]) | set(right_info["changed_files"])):
        baseline = left_data.get("baseline/" + path)
        before = left_data.get("changes/" + path, baseline)
        after = right_data.get("changes/" + path, baseline)
        if before == after:
            continue
        if before is not None:
            left_sources[path] = before
        updates[path] = after
        files.append({"path": path, "change": "added" if before is None else "removed" if after is None else "modified",
                      "left_sha256": file_sha(before) if before is not None else None,
                      "right_sha256": file_sha(after) if after is not None else None})

    identities = ("bundle_sha256", "source_run_id", "verification_id")
    return {
        "task_id": left_task.task_id, "base_revision": left_task.base_revision,
        "profile_sha256": left_candidate.profile_sha256,
        "left": {key: left_info[key] for key in identities},
        "right": {key: right_info[key] for key in identities},
        "candidate_content_equal": not files, "files": files, "diff": render_patch(left_sources, updates).decode("utf-8"),
    }
