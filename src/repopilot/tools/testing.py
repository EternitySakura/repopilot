"""Host supervisor for a fixed test profile. Target code runs only inside Docker."""

import io
import json
import re
import subprocess
import tarfile
import traceback
from hashlib import sha256
from pathlib import Path
from threading import Thread
from uuid import uuid4

from repopilot.agent.runtime import deadline_scope, remaining_timeout
from repopilot.contracts.tool_data import RunTestData
from repopilot.repository.profiles import FIXTURE_REPO, TARGET_TEST
from repopilot.tools.executions import ExecutionStore
from repopilot.tools.policy import ToolFailure
from repopilot.workspace import Workspace

# Controller-owned immutable image built and recorded on Day 1; never supplied by a model.
FIXTURE_IMAGE = FIXTURE_REPO.image_id  # Compatibility import; registration is the source of truth.
LOG_LIMIT = TARGET_TEST.log_limit_bytes


class DockerTests:
    execution_scope = "public_tests"

    def __init__(self, workspace: Workspace, artifacts: Path, *, timeout: int = 60):
        self.workspace = workspace
        self.artifacts = artifacts
        self.timeout = timeout
        self.repo_profile = workspace.profiles.repo
        self.test_profile = workspace.profiles.test
        self.executions = ExecutionStore(artifacts)
        self._active_execution: str | None = None
        self._source_snapshot: dict[str, bytes] | None = None
        self._blocked = False
        if timeout < 1 or timeout > workspace.profiles.budget.tool_timeout_seconds:
            raise ToolFailure("BUDGET_PROFILE_DENIED", "Test deadline exceeds the registered profile cap")

    @staticmethod
    def command(arguments: list[str], *, input_data: bytes | None = None, timeout: int = 60):
        try:
            return subprocess.run(["docker", *arguments], input=input_data, capture_output=True,
                                  timeout=remaining_timeout(timeout), check=False)
        except FileNotFoundError as exc:
            raise ToolFailure("DOCKER_UNAVAILABLE", "Docker CLI was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolFailure("DOCKER_UNAVAILABLE", "Docker control command timed out") from exc

    def context(self, base_image: str) -> bytes:
        """No user Dockerfile, hooks, dependencies, secrets or host mounts enter the build."""
        files = {name: self.workspace.original[name] for name in self.repo_profile.public_tests + ("pyproject.toml",)}
        if self._source_snapshot is not None:
            files.update(self._source_snapshot)
        else:
            for name in self.workspace.policy.public_sources:
                data, clipped = self.workspace.policy.read(name)
                if clipped:
                    raise ToolFailure("FILE_TOO_LARGE", "Candidate source exceeds test byte limit")
                files[name] = data
        files["Dockerfile"] = (
            f"FROM {base_image}\nUSER 0\n"
            'RUN ["rm", "-rf", "/fixture/src", "/fixture/tests"]\n'
            "USER 65534:65534\nCOPY src /fixture/src\nCOPY tests /fixture/tests\n"
            "COPY pyproject.toml /fixture/pyproject.toml\n"
            + "CMD " + json.dumps(["python", "-m", "pytest", *self.test_profile.pytest_args()]) + "\n"
        ).encode()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as stream:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                stream.addfile(info, io.BytesIO(data))
        return archive.getvalue()

    def stdin_payload(self) -> bytes | None:
        return None

    def input_bindings(self) -> dict:
        return {"public_tests": {name: sha256(self.workspace.original[name]).hexdigest()
                                 for name in self.repo_profile.public_tests},
                "config": {"pyproject.toml": sha256(self.workspace.original["pyproject.toml"]).hexdigest()}}

    def summarize_output(self, exit_code: int, text: str, clipped: bool) -> tuple[int, int, bool]:
        passed = re.findall(r"\b(\d+) passed\b", text)
        failed = re.findall(r"\b(\d+) failed\b", text)
        passed_count = int(passed[-1]) if passed else 0
        failed_count = int(failed[-1]) if failed else 0
        return (passed_count, failed_count, exit_code == 0 and passed_count == len(self.test_profile.expected_nodes)
                and failed_count == 0 and not clipped)

    def attached(self, name: str, log: Path, *, input_data: bytes | None = None) -> tuple[int, bool]:
        interactive = ["--interactive"] if input_data is not None else []
        process = subprocess.Popen(["docker", "start", "--attach", *interactive, name],
                                   stdin=subprocess.PIPE if interactive else None, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        clipped = False
        reader_errors = []

        def drain():
            nonlocal clipped
            try:
                remaining = self.test_profile.log_limit_bytes
                with log.open("wb") as stream:
                    while chunk := process.stdout.read(8192):
                        stream.write(chunk[:remaining])
                        if len(chunk) > remaining:
                            clipped = True
                        remaining = max(0, remaining - len(chunk))
            except Exception as exc:  # noqa: BLE001 - re-raised in the supervising thread below
                reader_errors.append(exc)

        reader = Thread(target=drain, daemon=True)
        reader.start()
        try:
            if input_data is not None:
                try:
                    process.stdin.write(input_data)
                    process.stdin.close()
                except BrokenPipeError:
                    pass  # Container exited early; use the recorded container status below.
            process.wait(timeout=remaining_timeout(self.timeout))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            reader.join(timeout=10)
            if reader.is_alive():
                raise RuntimeError("Docker log reader failed to stop")
            process.stdout.close()
        if reader_errors:
            raise reader_errors[0]
        return process.returncode, clipped

    def snapshot(self) -> dict[str, bytes]:
        changes = self.workspace.capture_changes()
        return {name: changes.get(name, self.workspace.original.get(name, b""))
                for name in sorted(self.workspace.policy.public_sources)}

    def status(self, execution_id: str, *, generation: int):
        # Validate the capability before touching any workspace content.
        self.executions.get(execution_id)
        hashes = {name: sha256(data).hexdigest() for name, data in self.snapshot().items()}
        return self.executions.status(execution_id, generation=generation, source_hashes=hashes)

    def _checkpoint(self, **changes):
        if self._active_execution is not None:
            self.executions.update(self._active_execution, **changes)

    def run(self, call_id, *, generation: int = 0) -> RunTestData:
        if self._blocked:
            raise RuntimeError("Unknown execution state; stop workspace use and do not retry")
        if self._active_execution is not None:
            raise RuntimeError("A test supervisor cannot run concurrent executions")
        sources = self.snapshot()
        hashes = {name: sha256(data).hexdigest() for name, data in sources.items()}
        inputs = {"version": "test-input-v1", "scope": self.execution_scope,
                  "task_id": self.workspace.task_id,
                  "base_revision": self.workspace.base_revision, "source_generation": generation,
                  "profile_sha256": self.workspace.profiles.sha256, "sources": hashes,
                  **self.input_bindings(),
                  "base_image_id": self.repo_profile.image_id}
        raw = (json.dumps(inputs, sort_keys=True) + "\n").encode()
        directory = self.artifacts / "tests" / str(call_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "input.manifest.json").write_bytes(raw)
        record = self.executions.begin(call_id, generation=generation, scope=self.execution_scope,
                                       input_sha256=sha256(raw).hexdigest(), source_hashes=hashes)
        self._active_execution = record.execution_id
        self._source_snapshot = sources
        try:
            try:
                tested = self._execute(call_id)
                current = self.executions.current(record.execution_id)
                self.executions.finish(record.execution_id, state="completed", exit_code=tested.exit_code,
                    passed=tested.passed, failed=tested.failed, public_tests_passed=tested.public_tests_passed)
            except (Exception, KeyboardInterrupt) as exc:
                current = self.executions.current(record.execution_id)
                unknown = current.cleanup_status in {"pending", "unknown"}
                self._blocked = unknown
                code = ("CLEANUP_UNKNOWN" if unknown else "USER_CANCELLED" if isinstance(exc, KeyboardInterrupt)
                        else exc.code if isinstance(exc, ToolFailure) else "PROGRAM_ERROR")
                state = "unknown" if unknown else "timed_out" if code == "TEST_TIMEOUT" else "failed"
                self.executions.finish(record.execution_id, state=state, error_code=code,
                                       cleanup_status="unknown" if unknown else current.cleanup_status,
                                       diagnostic=traceback.format_exc())
                if isinstance(exc, (FileNotFoundError, NotADirectoryError, PermissionError)):
                    # These types are ordinary path errors for repository reads, but a missing
                    # supervisor output after execution started is a fatal internal failure.
                    fatal = RuntimeError("Unexpected test supervisor file error; stop workspace use")
                    fatal.execution_id = record.execution_id
                    raise fatal from exc
                exc.execution_id = record.execution_id
                raise
            return tested.model_copy(update={"execution_id": record.execution_id,
                "input_manifest_sha256": record.input_manifest_sha256, "source_generation": generation,
                "cleanup_status": current.cleanup_status})
        finally:
            self._active_execution = None
            self._source_snapshot = None

    def _execute(self, call_id) -> RunTestData:
        directory = self.artifacts / "tests" / str(call_id)
        log = directory / "output.log"
        image_tag = f"repopilot-candidate:{uuid4().hex}"
        base_tag = f"repopilot-test-base:{uuid4().hex}"
        container = f"repopilot-test-{uuid4().hex}"
        # Mark each attempt BEFORE issuing the mutation: a lost response may still have created it.
        cleanup_commands = []
        mutation_uncertain = False

        def mutate(arguments, **kwargs):
            nonlocal mutation_uncertain
            try:
                return self.command(arguments, **kwargs)
            except (Exception, KeyboardInterrupt):
                # A timed-out Docker build can outlive its client and assign a tag later.
                mutation_uncertain = True
                raise

        try:
            available = self.command(["image", "inspect", self.repo_profile.image_id, "--format", "{{.Id}}"])
            if available.returncode:
                log.write_bytes(available.stderr[:LOG_LIMIT])
                raise ToolFailure("DOCKER_UNAVAILABLE", "The frozen Day 1 fixture image is unavailable",
                                  artifact_ref=str(log))
            if available.stdout.decode().strip() != self.repo_profile.image_id:
                raise ToolFailure("TEST_SETUP_FAILED", "Base image identity mismatch")
            # BuildKit treats a bare sha256 image ID in FROM as a registry name.
            # Give the verified local image a unique controller-owned tag; never pull it.
            self._checkpoint(phase="tagging", cleanup_status="pending",
                             resources={"base_tag": base_tag, "image_tag": image_tag, "container": container})
            cleanup_commands.append(["image", "rm", base_tag])
            tag = mutate(["tag", self.repo_profile.image_id, base_tag])
            if tag.returncode:
                raise ToolFailure("TEST_SETUP_FAILED", "Cannot tag frozen local base image")
            context = self.context(base_tag)
            self._checkpoint(phase="building", context_sha256=sha256(context).hexdigest())
            cleanup_commands.insert(0, ["image", "rm", image_tag])
            build = mutate(["build", "--network", "none", "--pull=false", "-t", image_tag, "-"],
                                 input_data=context, timeout=120)
            (directory / "build.log").write_bytes((build.stdout + build.stderr)[:LOG_LIMIT])
            if build.returncode:
                raise ToolFailure("TEST_SETUP_FAILED", "Candidate image build failed",
                                  artifact_ref=str(directory / "build.log"))
            image = self.command(["image", "inspect", image_tag, "--format", "{{.Id}}"])
            image_id = image.stdout.decode().strip()
            if image.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise ToolFailure("TEST_SETUP_FAILED", "Cannot resolve candidate image ID")
            self._checkpoint(phase="creating")
            cleanup_commands.insert(0, ["rm", "--force", container])
            payload = self.stdin_payload()
            interactive = ["--interactive"] if payload is not None else []
            create = mutate([
                "create", "--name", container, *interactive, *self.test_profile.container_args(), image_id,
            ])
            if create.returncode:
                log.write_bytes(create.stderr[:LOG_LIMIT])
                raise ToolFailure("TEST_SETUP_FAILED", "Cannot create test container", artifact_ref=str(log))
            self._checkpoint(phase="testing", state="running")
            try:
                if payload is None:
                    _, clipped = self.attached(container, log)
                else:
                    _, clipped = self.attached(container, log, input_data=payload)
            except subprocess.TimeoutExpired as exc:
                raise ToolFailure("TEST_TIMEOUT", "Test deadline exceeded; container forcibly removed",
                                  artifact_ref=str(log)) from exc
            state = self.command(["inspect", container, "--format", "{{json .State}}"])
            try:
                observed = json.loads(state.stdout)
            except (ValueError, UnicodeError):
                observed = None
            if (state.returncode or not isinstance(observed, dict) or observed.get("Running") is not False
                    or observed.get("Status") != "exited" or type(observed.get("ExitCode")) is not int
                    or observed.get("OOMKilled") is not False or observed.get("Error")):
                raise ToolFailure("TEST_SETUP_FAILED", "Container exit state was not confirmed")
            exit_code = observed["ExitCode"]
            text = log.read_text(encoding="utf-8", errors="replace")
            passed_count, failed_count, passed = self.summarize_output(exit_code, text, clipped)
            return RunTestData(exit_code=exit_code, passed=passed_count, failed=failed_count,
                               public_tests_passed=passed,
                               log_ref=str(log), image_id=image_id, truncated=clipped)
        finally:
            # Runtime expiry must not prevent removal and confirmation of resources.
            with deadline_scope(None):
                self._cleanup(cleanup_commands, mutation_uncertain)

    def _cleanup(self, cleanup_commands, mutation_uncertain):
        self._checkpoint(phase="cleanup")
        failures = []
        for arguments in cleanup_commands:
            try:
                cleanup = self.command(arguments)
                if cleanup.returncode:
                    # A failed create/build may leave nothing to remove. Require a successful
                    # inventory response proving absence; failed inspect alone is ambiguous.
                    target = arguments[-1]
                    query = (["container", "ls", "--all", "--filter", f"name=^/{target}$", "--format", "{{.Names}}"]
                             if arguments[0] == "rm" else
                             ["image", "ls", "--all", "--filter", f"reference={target}",
                              "--format", "{{.Repository}}:{{.Tag}}"])
                    inventory = self.command(query)
                    if inventory.returncode or inventory.stdout.strip():
                        failures.append(" ".join(arguments))
            except Exception:  # noqa: BLE001 - try all cleanup operations, retain diagnostics and fail below
                failures.append(traceback.format_exc())
        if failures or mutation_uncertain:
            self._checkpoint(cleanup_status="unknown")
            # Unknown container state must be fatal, even if the Docker API failure is expected.
            raise RuntimeError(f"Docker cleanup failed or mutation outcome unknown; stop workspace use: {failures}")
        self._checkpoint(cleanup_status="confirmed" if cleanup_commands else "not_needed")
