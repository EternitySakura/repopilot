"""Rebuild public Gate A from frozen artifacts, without consulting Coder workspace or logs."""

import hashlib
import re
import stat
import traceback
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import ValidationError

from repopilot.contracts.candidate import CandidateManifest
from repopilot.contracts.evaluation import EvaluationContext
from repopilot.contracts.task import TaskSpec
from repopilot.contracts.verification import (
    EXPECTED_TESTS,
    TestRecord,
    VerificationReport,
)
from repopilot.observability.events import EventLog
from repopilot.repository.profiles import FIXTURE_REPO, resolve_profiles
from repopilot.repository.tasks import DEFAULT_TASK_ID, fixture_root, load_task
from repopilot.settings import Settings
from repopilot.tools.edit import Editor
from repopilot.tools.policy import ReadPolicy, ToolFailure
from repopilot.tools.testing import FIXTURE_IMAGE
from repopilot.verification.docker import GateADockerTests, read_test_record
from repopilot.workspace import Workspace, render_patch

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = PROJECT_ROOT / FIXTURE_REPO.fixture_directory
ARTIFACT_LIMIT = 2 * 1024 * 1024


@dataclass(frozen=True)
class CandidateSnapshot:
    task: TaskSpec
    patch: bytes
    manifest: bytes


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_artifact(directory: Path, name: str) -> bytes:
    root = directory.absolute()
    for node in (*reversed(root.parents), root, root / name):
        ReadPolicy._check_node(node)
    path = root / name
    if not stat.S_ISREG(path.stat().st_mode):
        raise ToolFailure("PATH_DENIED", "Candidate artifact must be a regular file")
    with path.open("rb") as stream:
        content = stream.read(ARTIFACT_LIMIT + 1)
    if len(content) > ARTIFACT_LIMIT:
        raise ToolFailure("ARTIFACT_TOO_LARGE", "Candidate artifact exceeds 2 MiB")
    return content


def rebuild_candidate(workspace: Workspace, task: TaskSpec, manifest: CandidateManifest,
                      patch: bytes, events: EventLog) -> dict[str, str]:
    if manifest.task_id is not None and manifest.task_id != task.task_id:
        raise ToolFailure("CANDIDATE_TASK_MISMATCH", "Candidate belongs to a different task")
    if manifest.task_id is None and task.task_id != DEFAULT_TASK_ID:
        raise ToolFailure("MISSING_TASK_BINDING", "Only the original pinned task accepts legacy unbound candidates")
    if manifest.profile_sha256 is None and task.task_id != DEFAULT_TASK_ID:
        raise ToolFailure("MISSING_PROFILE_BINDING", "New tasks require a versioned profile binding")
    if manifest.base_revision != task.base_revision:
        raise ToolFailure("CANDIDATE_BASELINE_MISMATCH", "Candidate was exported against a different baseline")
    if manifest.profile_sha256 is not None and manifest.profile_sha256 != workspace.profiles.sha256:
        raise ToolFailure("PROFILE_MISMATCH", "Candidate profile snapshot differs from the registered versions")
    if sha256(patch) != manifest.patch_sha256:
        raise ToolFailure("PATCH_HASH_MISMATCH", "Patch differs from its frozen manifest")
    if not manifest.changes:
        raise ToolFailure("NO_CHANGES", "No candidate source changes to verify")
    editor = Editor(workspace.policy, task.editable_globs, events)
    changes = {}
    seen = set()
    destinations = {}
    # Validate the entire package before applying any changes to the disposable cleanroom.
    for change in manifest.changes:
        name = change.path
        if PurePosixPath(name).as_posix() != name or name.casefold() in seen:
            raise ToolFailure("INVALID_CANDIDATE_PATH", "Paths must be canonical and unique")
        seen.add(name.casefold())
        editor.check_scope(name)
        old = workspace.original.get(name)
        before = sha256(old) if old is not None else None
        if before != change.before_sha256:
            raise ToolFailure("HASH_CONFLICT", "Candidate before-hash does not match the frozen baseline")
        path = workspace.policy.check(name, additional_public_files=frozenset({name}),
                                      allow_missing_leaf=old is None)
        if old is None and path.exists():
            raise ToolFailure("ALREADY_EXISTS", "New source path aliases an existing file")
        try:
            content = change.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ToolFailure("NOT_TEXT", "Candidate must be valid UTF-8") from exc
        if b"\x00" in content:
            raise ToolFailure("NOT_TEXT", "Binary source changes are not accepted")
        if len(content) > workspace.policy.max_bytes:
            raise ToolFailure("FILE_TOO_LARGE", "Candidate source exceeds the file byte limit")
        if sha256(content) != change.after_sha256:
            raise ToolFailure("HASH_CONFLICT", "Candidate content does not match its after-hash")
        if old == content:
            raise ToolFailure("NO_CHANGES", "Manifest contains an unchanged source file")
        changes[name] = content
        destinations[name] = path
    if render_patch(workspace.original, changes) != patch:
        raise ToolFailure("PATCH_FORMAT_MISMATCH", "Diff and structured candidate describe different changes")
    for name, content in changes.items():
        # These paths belong solely to a fresh controller-created cleanroom.
        with destinations[name].open("wb" if name in workspace.original else "xb") as stream:
            stream.write(content)
        workspace.policy.public_sources |= {name}
    hashes = {}
    for name in sorted(workspace.policy.public_sources):
        data, clipped = workspace.policy.read(name)
        if clipped:
            raise ToolFailure("FILE_TOO_LARGE", "Reconstructed source exceeds the file byte limit")
        hashes[name] = sha256(data)
        if name in changes and hashes[name] != sha256(changes[name]):
            raise ToolFailure("HASH_CONFLICT", "Reconstructed file hash differs from the candidate")
    return hashes


def check_test_record(record: TestRecord, exit_code: int, *, expected_tests=EXPECTED_TESTS) -> tuple[str, list[str]]:
    if record.exit_code != exit_code:
        return "error", ["EXIT_CODE_MISMATCH"]
    if exit_code == 5:
        return "error", ["ZERO_TESTS"]
    if exit_code == 2:
        return "error", ["COLLECTION_ERROR"]
    if exit_code not in {0, 1}:
        return "error", ["TEST_ENVIRONMENT_ERROR"]
    if not record.collected:
        return "error", ["ZERO_TESTS"]
    if sorted(record.collected) != sorted(expected_tests):
        return "failed_checks", ["TEST_SET_MISMATCH"]
    if any(phase.outcome == "skipped" for phase in record.reports):
        return "failed_checks", ["UNEXPECTED_SKIP"]
    expected_phases = {(node, phase) for node in expected_tests for phase in ("setup", "call", "teardown")}
    actual = [(phase.nodeid, phase.when) for phase in record.reports]
    if len(actual) != len(set(actual)) or set(actual) != expected_phases:
        return "error", ["INCOMPLETE_TEST_RECORD"]
    if any(phase.outcome == "failed" and phase.when != "call" for phase in record.reports):
        return "error", ["TEST_EXECUTION_ERROR"]
    failed = any(phase.outcome == "failed" for phase in record.reports)
    if (exit_code == 0) == failed:
        return "error", ["EXIT_CODE_MISMATCH"]
    return ("failed_checks", ["PUBLIC_TEST_FAILED"]) if failed else ("passed", [])


def verify_candidate(candidate_dir: Path, runs: Path | None = None, *,
                     settings: Settings | None = None, task_id: str | None = None,
                     evaluation_context: EvaluationContext | None = None,
                     snapshot: CandidateSnapshot | None = None) -> VerificationReport:
    config = settings if settings is not None else Settings()
    events = EventLog(runs if runs is not None else PROJECT_ROOT / "artifacts" / "verifications")
    artifacts = {"events": str(events.path)}
    result = {
        "verification_id": events.run_id, "source_run_id": candidate_dir.name,
        "status": "error", "failed_checks": [], "cleanroom_verified": False,
        "base_revision": None, "patch_sha256": None, "manifest_sha256": None,
        "environment_digest": FIXTURE_IMAGE, "candidate_image_id": None,
        "source_hashes": {}, "original_test_hashes": {}, "test_record": None, "test_attempts": 0,
        "artifacts": artifacts, "message": "", "diagnostic": None,
    }
    events.write("run.started", mode="cleanroom_public_gate_a", source_run_id=candidate_dir.name)
    try:
        patch = snapshot.patch if snapshot is not None else read_artifact(candidate_dir, "candidate.patch")
        raw = snapshot.manifest if snapshot is not None else read_artifact(candidate_dir, "candidate.manifest.json")
        manifest = CandidateManifest.model_validate_json(raw)
        task = snapshot.task if snapshot is not None else load_task(task_id if task_id is not None else manifest.task_id or DEFAULT_TASK_ID)
        if evaluation_context is not None:
            if evaluation_context.task_id != task.task_id:
                raise ToolFailure("CANDIDATE_TASK_MISMATCH", "Evaluation context belongs to another task")
            events.bind_evaluation(evaluation_context)
            result["evaluation_context"] = evaluation_context.model_dump(mode="json")
        events.register_task(task)
        profiles = resolve_profiles(task)
        profiles.budget.check_settings(config)
        result["profile_sha256"] = profiles.sha256
        result["environment_digest"] = profiles.repo.image_id
        result["base_revision"] = task.base_revision
        result["task_id"] = task.task_id
        result["verification_scope"] = {"repo_profile": profiles.repo.name,
                                        "expected_tests": list(profiles.test.expected_nodes)}
        result.update(patch_sha256=sha256(patch), manifest_sha256=sha256(raw))
        events.write("verification.intent", source_run_id=candidate_dir.name, scope="public_gate_a",
                     patch_sha256=sha256(patch), manifest_sha256=sha256(raw), base_revision=task.base_revision)
        inputs = events.directory / "inputs"
        inputs.mkdir()
        (inputs / "candidate.patch").write_bytes(patch)
        (inputs / "candidate.manifest.json").write_bytes(raw)
        artifacts.update(patch=str(inputs / "candidate.patch"), manifest=str(inputs / "candidate.manifest.json"))
        clean = Workspace(fixture_root(task), events.directory / "cleanroom", task)
        clean.profiles.save_snapshot(events.directory, settings=config)
        artifacts["profiles"] = str(events.directory / "profiles.json")
        result["original_test_hashes"] = {name: sha256(data) for name, data in clean.original.items()
                                          if name.startswith("tests/")}
        result["source_hashes"] = rebuild_candidate(clean, task, manifest, patch, events)
        events.write("verification.rebuilt", source_hashes=result["source_hashes"],
                     original_test_hashes=result["original_test_hashes"])
        runner = GateADockerTests(clean, events.directory, timeout=config.tool_timeout_seconds)
        runner.executions.bind(events)
        artifacts["executions"] = str(events.directory / "executions.json")
        call_id = uuid4()
        events.write("tool.intent", tool_call_id=call_id, action_type="gate_a_public_tests")
        result["test_attempts"] = 1
        try:
            tested = runner.run(call_id)
        except (Exception, KeyboardInterrupt) as exc:
            execution_id = getattr(exc, "execution_id", None)
            if execution_id is not None:
                record = runner.executions.current(execution_id)
                result.update(execution_id=execution_id, input_manifest_sha256=record.input_manifest_sha256)
                artifacts["test_inputs"] = str(events.directory / "tests" / str(call_id) / "input.manifest.json")
            events.write("tool.failed", tool_call_id=call_id, action_type="gate_a_public_tests",
                         diagnostic=traceback.format_exc(), execution_id=execution_id)
            raise
        events.write("tool.completed", tool_call_id=call_id, action_type="gate_a_public_tests",
                     result=tested.model_dump(mode="json"))
        result["candidate_image_id"] = tested.image_id
        result["execution_id"] = tested.execution_id
        result["input_manifest_sha256"] = tested.input_manifest_sha256
        artifacts["test_inputs"] = str(events.directory / "tests" / str(call_id) / "input.manifest.json")
        artifacts["test_log"] = tested.log_ref
        if tested.truncated:
            raise ToolFailure("OUTPUT_LIMIT", "Structured test evidence was truncated")
        record = read_test_record(Path(tested.log_ref).read_text(encoding="utf-8", errors="replace"))
        status, checks = check_test_record(record, tested.exit_code, expected_tests=clean.profiles.test.expected_nodes)
        result.update(status=status, failed_checks=checks, cleanroom_verified=status == "passed",
                      test_record=record, message="Clean public Gate A completed; final Gate B was not performed")
    except KeyboardInterrupt as exc:
        result.update(status="cancelled", failed_checks=["USER_CANCELLED"],
                      execution_id=getattr(exc, "execution_id", result.get("execution_id")),
                      message="Public Gate A cancelled; candidate and execution evidence retained")
    except (ToolFailure, ValidationError, FileNotFoundError, PermissionError, NotADirectoryError) as exc:
        result["execution_id"] = getattr(exc, "execution_id", result.get("execution_id"))
        code = exc.code if isinstance(exc, ToolFailure) else (
            "INVALID_MANIFEST" if isinstance(exc, ValidationError) else "ARTIFACT_UNAVAILABLE")
        environment = {"DOCKER_UNAVAILABLE", "TEST_SETUP_FAILED", "OUTPUT_LIMIT", "INVALID_TEST_RESULT",
                       "ARTIFACT_UNAVAILABLE", "BASELINE_MISMATCH", "PROFILE_DENIED", "BUDGET_PROFILE_DENIED", "TASK_DENIED"}
        status = "timed_out" if code == "TEST_TIMEOUT" else "error" if code in environment else "failed_checks"
        result.update(status=status, failed_checks=[code], message=str(exc))
        if isinstance(exc, ToolFailure) and exc.artifact_ref:
            artifacts["test_log"] = exc.artifact_ref
    except Exception as exc:
        result["execution_id"] = getattr(exc, "execution_id", result.get("execution_id"))
        result.update(status="error", failed_checks=["PROGRAM_ERROR"], message="Unexpected verifier error",
                      diagnostic=traceback.format_exc())
        save_verification(events, result)
        raise
    return save_verification(events, result)


def save_verification(events: EventLog, result: dict) -> VerificationReport:
    report = VerificationReport.model_validate(result)
    (events.directory / "verification.json").write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    lines = ["# RepoPilot 干净环境 Gate A 验证", "", f"- 验证 ID：`{report.verification_id}`",
             f"- 候选运行：`{report.source_run_id}`", f"- 结果：`{report.status}`",
             "- 范围：冻结 Fixture 基线、授权源码变化、原始公开测试及固定测试集合。",
             "- 不包含隐藏测试、额外回归套件或最终 Gate B；不是最终 Benchmark 成功。",
             f"- 检查失败：{', '.join(report.failed_checks) or '无'}", f"- 说明：{report.message}",
             f"- 补丁 SHA-256：`{report.patch_sha256}`", f"- 清单 SHA-256：`{report.manifest_sha256}`",
             f"- 测试执行 ID：`{report.execution_id}`", f"- 测试输入清单 SHA-256：`{report.input_manifest_sha256}`",
             "", "## 证据", ""]
    for name, path in report.artifacts.items():
        href = path.replace("\\", "/")
        lines.append(f"- [{name}](<{href}>)")
    if report.diagnostic:
        lines.extend(["", "```text", report.diagnostic.rstrip(), "```"])
    (events.directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    events.write("run.finished", status=report.status, verification_scope=report.verification_scope.model_dump(),
                 cleanroom_verified=report.cleanroom_verified, benchmark_success=False,
                 failed_checks=report.failed_checks, report_ref=str(events.directory / "report.md"),
                 report=report.model_dump(mode="json"))
    return report


def verify_run(run_id: str, *, settings: Settings | None = None, task_id: str | None = None) -> VerificationReport:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("run ID must be 32 lowercase hexadecimal characters")
    return verify_candidate(PROJECT_ROOT / "artifacts" / "runs" / run_id, settings=settings, task_id=task_id)
