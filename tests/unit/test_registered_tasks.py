import io
import json
import tarfile
from fnmatch import fnmatchcase
from unittest.mock import Mock, patch

import pytest

from repopilot.agent.live import run_live
from repopilot.agent.live_acceptance import fixture_hashes
from repopilot.cli import main
from repopilot.observability.events import EventLog
from repopilot.repository.profiles import resolve_profiles
from repopilot.repository.tasks import (
    DEVELOPMENT_TASK_IDS,
    TASK_FILES,
    fixture_root,
    load_task,
)
from repopilot.tools.policy import ToolFailure
from repopilot.tools.testing import DockerTests
from repopilot.verification.gate_a import verify_candidate
from repopilot.workspace import Workspace


@pytest.mark.parametrize("identity", tuple(TASK_FILES))
def test_registered_inputs_are_frozen_and_only_the_selected_task_is_readable(tmp_path, identity):
    task = load_task(identity)
    workspace = Workspace(fixture_root(task), tmp_path / "workspace", task)
    assert set(workspace.original) == set(fixture_hashes(identity))
    assert workspace.policy.read(workspace.profiles.repo.public_tests[0])[0]
    assert "verifier_profile" not in task.agent_view().model_dump()
    for other in TASK_FILES:
        if other != identity:
            source = resolve_profiles(load_task(other)).repo.public_sources[0]
            with pytest.raises(ToolFailure, match="public read scope"):
                workspace.policy.read(source)
    with tarfile.open(fileobj=io.BytesIO(DockerTests(workspace, tmp_path).context("controller-base"))) as archive:
        assert {name for name in archive.getnames() if name.startswith("src/")} == set(workspace.policy.public_sources)
        assert {name for name in archive.getnames() if name.startswith("tests/")} == set(workspace.profiles.repo.public_tests)
        dockerfile = archive.extractfile("Dockerfile").read()
        assert b'RUN ["rm", "-rf", "/fixture/src", "/fixture/tests"]' in dockerfile
        assert b"USER 65534:65534" in dockerfile


@pytest.mark.parametrize("identity", ["../rp-easy-001", "tasks/rp-easy-001.json", "C:/tasks/other", "rp-easy-999", ""])
def test_unknown_ids_cannot_touch_docker_or_provider_or_create_a_run(tmp_path, identity):
    supplier = Mock()
    with patch.object(DockerTests, "command") as docker, pytest.raises(ToolFailure) as error:
        run_live(task_id=identity, runs=tmp_path / "runs", transport=supplier)
    assert error.value.code == "TASK_DENIED"
    docker.assert_not_called()
    supplier.assert_not_called()
    assert not (tmp_path / "runs").exists()


def frozen_candidate(tmp_path, identity):
    task = load_task(identity)
    events = EventLog(tmp_path / "runs")
    workspace = Workspace(fixture_root(task), events.directory / "workspace", task)
    # These tests exercise routing and candidate binding across both splits.
    # A benign source change suffices without needing a repair recipe.
    path = next(name for name in workspace.profiles.repo.public_sources
                if any(fnmatchcase(name, glob) for glob in task.editable_globs))
    (workspace.root / path).write_bytes(workspace.original[path] + b"\n# Candidate under test\n")
    workspace.export_diff(events.directory / "candidate.patch")
    return events.directory


@pytest.mark.parametrize("identity", tuple(TASK_FILES))
def test_gate_routes_frozen_candidate_without_consulting_mutable_run_metadata(tmp_path, identity):
    directory = frozen_candidate(tmp_path, identity)
    (directory / "run.json").write_text("untrusted mutable run metadata", encoding="utf-8")
    with patch("repopilot.verification.gate_a.GateADockerTests.run", side_effect=ToolFailure("DOCKER_UNAVAILABLE", "unit")) as run:
        report = verify_candidate(directory, tmp_path / "verifications")
    assert run.call_count == 1
    assert report.task_id == identity and report.failed_checks == ["DOCKER_UNAVAILABLE"]
    profile = resolve_profiles(load_task(identity))
    assert report.verification_scope.repo_profile == profile.repo.name
    assert report.verification_scope.expected_tests == list(profile.test.expected_nodes)


def test_candidates_and_verifiers_cannot_be_crossed_between_tasks(tmp_path):
    directory = frozen_candidate(tmp_path, "rp-easy-002")
    with patch("repopilot.verification.gate_a.GateADockerTests.run") as run:
        report = verify_candidate(directory, tmp_path / "gate", task_id="rp-easy-003")
    run.assert_not_called()
    assert report.failed_checks == ["CANDIDATE_TASK_MISMATCH"] and report.test_attempts == 0
    task = load_task("rp-easy-002")
    with pytest.raises(ToolFailure) as error:
        resolve_profiles(task.model_copy(update={"verifier_profile": "fixture_mapping_gate_a"}))
    assert error.value.code == "PROFILE_DENIED"


@pytest.mark.parametrize(("field", "code"), [("task_id", "MISSING_TASK_BINDING"), ("profile_sha256", "MISSING_PROFILE_BINDING")])
def test_new_candidates_require_task_and_profile_bindings(tmp_path, field, code):
    directory = frozen_candidate(tmp_path, "rp-easy-002")
    manifest_path = directory / "candidate.manifest.json"
    data = json.loads(manifest_path.read_text())
    data.pop(field)
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with patch("repopilot.verification.gate_a.GateADockerTests.run") as run:
        report = verify_candidate(directory, tmp_path / "gate", task_id="rp-easy-002")
    run.assert_not_called()
    assert report.failed_checks == [code]


def test_cli_lists_public_registrations_and_rejects_ambiguous_selection(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["repopilot", "--list-tasks"])
    assert main() == 0
    output = capsys.readouterr().out
    assert {row["task_id"] for row in json.loads(output)} == set(DEVELOPMENT_TASK_IDS)
    assert "verifier_profile" not in output
    for args in (["--task-id", "rp-easy-002"], ["--list-tasks", "--fake-demo"],
                 ["--live-run", "--task-id", "../escape"]):
        monkeypatch.setattr("sys.argv", ["repopilot", *args])
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2
