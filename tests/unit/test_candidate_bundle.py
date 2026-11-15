import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from repopilot.contracts.candidate import CandidateManifest
from repopilot.ledger.store import digest
from repopilot.repository.tasks import PROJECT_ROOT, fixture_root, load_task
from repopilot.verification.bundle import (
    BundleEnvelope,
    BundleManifest,
    PublicVerification,
    check_bundle,
    export_bundle,
    file_sha,
    materialize_bundle,
)
from repopilot.workspace import Workspace, render_patch


@pytest.fixture
def bundle(tmp_path):
    task = load_task()
    profiles, original = Workspace.read_original(fixture_root(task), task)
    path = "src/fixture_text/parser.py"
    content = original[path].replace(b"if text[0]", b"if text and text[0]")
    patch_bytes = render_patch(original, {path: content})
    candidate = CandidateManifest(task_id=task.task_id, base_revision=task.base_revision, profile_sha256=profiles.sha256,
        patch_sha256=file_sha(patch_bytes), changes=[{"path": path, "before_sha256": file_sha(original[path]),
            "after_sha256": file_sha(content), "content": content.decode()}])
    raw = candidate.model_dump_json(indent=2).encode()
    gate = PublicVerification(source_run_id="a" * 32, verification_id="b" * 32, task_id=task.task_id,
        patch_sha256=candidate.patch_sha256, manifest_sha256=file_sha(raw), profile_sha256=profiles.sha256,
        environment_digest=profiles.repo.image_id, expected_tests=list(profiles.test.expected_nodes),
        test_record={"exit_code": 0, "collected": list(profiles.test.expected_nodes), "reports": [
            {"nodeid": node, "when": phase, "outcome": "passed"} for node in profiles.test.expected_nodes
            for phase in ("setup", "call", "teardown")]})
    directory = tmp_path / "bundle"
    directory.mkdir()
    data = {"task.json": task.agent_view().model_dump_json().encode(), "candidate.patch": patch_bytes,
            "candidate.manifest.json": raw, "verification.json": gate.model_dump_json().encode(),
            "changes/" + path: content, **{"baseline/" + name: value for name, value in original.items()}}
    for name, value in data.items():
        destination = directory / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value)
    manifest = BundleManifest(source_run_id=gate.source_run_id, verification_id=gate.verification_id,
        files={name: file_sha(value) for name, value in data.items()}, verification_history=[
            {"verification_id": "c" * 32, "status": "failed_checks", "patch_sha256": candidate.patch_sha256},
            {"verification_id": gate.verification_id, "status": "passed", "patch_sha256": candidate.patch_sha256}])
    save_manifest(directory, manifest)
    return directory


def save_manifest(directory, manifest):
    envelope = BundleEnvelope(sha256=digest(manifest.model_dump(mode="json")), manifest=manifest)
    (directory / "bundle.json").write_text(envelope.model_dump_json(), encoding="utf-8")


def test_offline_integrity_keeps_failed_history_and_runs_nothing(bundle):
    before = {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    with patch("repopilot.ledger.store.Ledger", side_effect=AssertionError("No database")), \
         patch("subprocess.run", side_effect=AssertionError("No process")):
        result = check_bundle(bundle)
    assert result["valid"] and not result["authenticity_verified"] and not result["final_gate_b"]
    assert len(result["verification_history"]) == 2 and result["new_test_runs"] == result["model_requests"] == 0
    assert before == {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}


@pytest.mark.parametrize("damage", ["content", "missing", "extra", "manifest", "gate_binding", "gate_failure", "baseline"])
def test_offline_check_rejects_changed_contents_or_relationships(bundle, damage):
    envelope = BundleEnvelope.model_validate_json((bundle / "bundle.json").read_bytes())
    if damage == "content":
        (bundle / "candidate.patch").write_bytes(b"altered")
    elif damage == "missing":
        (bundle / "candidate.patch").unlink()
    elif damage == "extra":
        (bundle / ".env").write_text("must not be included", encoding="utf-8")
    elif damage == "manifest":
        value = json.loads((bundle / "bundle.json").read_bytes())
        value["sha256"] = "0" * 64
        (bundle / "bundle.json").write_text(json.dumps(value), encoding="utf-8")
    else:
        name = "baseline/src/fixture_text/parser.py" if damage == "baseline" else "verification.json"
        if damage == "baseline":
            raw = b"changed baseline"
        else:
            value = json.loads((bundle / name).read_bytes())
            if damage == "gate_binding":
                value["source_run_id"] = "d" * 32
            else:
                value["test_record"]["reports"][0]["outcome"] = "failed"
            raw = json.dumps(value).encode()
        (bundle / name).write_bytes(raw)
        envelope.manifest.files[name] = file_sha(raw)
        save_manifest(bundle, envelope.manifest)  # Hashes alone must not bypass relationship checks.
    with pytest.raises((ValueError, FileNotFoundError)):
        check_bundle(bundle)


def test_native_check_does_not_need_a_database_or_api_key(bundle):
    environment = dict(os.environ, REPOPILOT_LEDGER_URL="invalid-database-url", LLM_API_KEY="")
    child = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts/export_candidate.py"), "--check", str(bundle)],
                           env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    assert child.returncode == 0, child.stdout + child.stderr
    assert json.loads(child.stdout)["valid"]


def test_export_refuses_active_source_before_writing(tmp_path):
    class Ledger:
        def summary(self, _):
            return {"status": "paused", "unresolved_calls": []}

    destination = tmp_path / "output"
    with pytest.raises(ValueError, match="stopped source"):
        export_bundle(Ledger(), "a" * 32, "b" * 32, destination)
    assert not destination.exists()


@pytest.mark.parametrize("failure", ["unresolved", "failed_gate", "audit"])
def test_export_rejects_unresolved_or_unverified_evidence_without_output(tmp_path, failure):
    class Ledger:
        def summary(self, identity):
            if identity == "source":
                return {"status": "failed", "unresolved_calls": ["unknown"] if failure == "unresolved" else [],
                        "artifact_directory": str(tmp_path / "source")}
            return {"mode": "cleanroom_public_gate_a", "status": "failed_checks" if failure == "failed_gate" else "passed",
                    "artifact_directory": str(tmp_path / "gate")}

    destination = tmp_path / "output"
    message = {"unresolved": "resolved calls", "failed_gate": "passing independent", "audit": "failed ledger reconciliation"}
    with patch("repopilot.verification.bundle.audit_run", return_value={"passed": False, "unresolved_calls": []}), \
         pytest.raises(ValueError, match=message[failure]):
        export_bundle(Ledger(), "source", "gate", destination)
    assert not destination.exists()


def test_materialized_copy_contains_exact_candidate_and_unchanged_public_tests(bundle, tmp_path):
    before = {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    destination = tmp_path / "review"
    with patch("subprocess.run", side_effect=AssertionError("No code execution")):
        result = materialize_bundle(bundle, destination)
    assert result["valid"] and not result["public_tests_executed"]
    for name in result["changed_files"]:
        assert (destination / name).read_bytes() == (bundle / "changes" / name).read_bytes()
    for test in (bundle / "baseline/tests").iterdir():
        assert (destination / "tests" / test.name).read_bytes() == test.read_bytes()
    assert before == {p: p.read_bytes() for p in bundle.rglob("*") if p.is_file()}


@pytest.mark.parametrize("mode", ["existing", "inside_bundle", "corrupt"])
def test_materialize_preserves_existing_inputs_and_rejects_invalid_package(bundle, tmp_path, mode):
    destination = bundle / "review" if mode == "inside_bundle" else tmp_path / "review"
    if mode == "existing":
        destination.mkdir()
        (destination / "keep.txt").write_text("keep", encoding="utf-8")
    if mode == "corrupt":
        (bundle / "candidate.patch").write_bytes(b"changed")
    with pytest.raises((ValueError, FileExistsError)):
        materialize_bundle(bundle, destination)
    if mode == "existing":
        assert (destination / "keep.txt").read_text(encoding="utf-8") == "keep"
    else:
        assert not destination.exists()
