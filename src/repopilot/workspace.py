"""Build a candidate only from verified, public fixture inputs; export all source changes."""

import difflib
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from repopilot.contracts.candidate import (
    MAX_CANDIDATE_FILES,
    CandidateChange,
    CandidateManifest,
)
from repopilot.contracts.task import TaskSpec
from repopilot.repository.profiles import resolve_profiles
from repopilot.tools.edit import check_edit_scope
from repopilot.tools.policy import ReadPolicy, ToolFailure


def render_patch(original: dict[str, bytes], changes: Mapping[str, bytes | None]) -> bytes:
    """One deterministic format for the human diff and the independently checked manifest."""
    chunks = []
    for name, new in sorted(changes.items()):
        old = original.get(name)
        if old == new:
            continue
        chunks.append(f"diff --git a/{name} b/{name}\n")
        if old is None:
            chunks.append("new file mode 100644\n")
        if new is None:
            chunks.append("deleted file mode 100644\n")
        lines = difflib.unified_diff(
            (old or b"").decode("utf-8").splitlines(keepends=True),
            (new or b"").decode("utf-8").splitlines(keepends=True),
            fromfile=f"a/{name}" if old is not None else "/dev/null",
            tofile=f"b/{name}" if new is not None else "/dev/null",
        )
        for line in lines:
            chunks.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(chunks).encode("utf-8")


class Workspace:
    def __init__(self, fixture: Path, destination: Path, task: TaskSpec):
        self.task_id = task.task_id
        self.base_revision = task.base_revision
        self.profiles, self.original = self.read_original(fixture, task)
        self.editable_globs = tuple(task.editable_globs)
        destination.mkdir(parents=True, exist_ok=False)
        self.root = destination
        for name, content in self.original.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.policy = ReadPolicy(destination, profile=self.profiles.repo)
        self.profiles.save_snapshot(destination.parent)

    @staticmethod
    def read_original(fixture: Path, task: TaskSpec):
        profiles = resolve_profiles(task)
        source = ReadPolicy(fixture, profile=profiles.repo)
        manifest = json.loads((fixture / "baseline.json").read_text(encoding="utf-8"))
        revision = hashlib.sha256(json.dumps(manifest["files"], sort_keys=True).encode()).hexdigest()
        if revision != task.base_revision or revision != manifest["sha256"]:
            raise ToolFailure("BASELINE_MISMATCH", "Task and fixture manifest revisions differ")
        # Controller-owned public input list; the task/model cannot select private tests.
        allowed = profiles.repo.public_files
        if set(manifest["files"]) != allowed:
            raise ToolFailure("BASELINE_MISMATCH", "Unexpected files in public fixture manifest")
        original = {}
        for name in sorted(allowed):
            content, clipped = source.read(name)
            if clipped or hashlib.sha256(content).hexdigest() != manifest["files"][name]:
                raise ToolFailure("BASELINE_MISMATCH", f"Fixture file differs from baseline: {name}")
            original[name] = content
        return profiles, original

    def _check_layout(self, names: set[str]) -> None:
        directories = {parent.as_posix() for name in names for parent in PurePosixPath(name).parents
                       if parent.as_posix() != "."}
        self.policy.check(".")
        visited = 0

        def walk(directory: Path):
            nonlocal visited
            for path in directory.iterdir():
                visited += 1
                if visited > self.profiles.repo.max_search_entries:
                    raise ToolFailure("OUTPUT_LIMIT", "Workspace layout exceeds the inspection limit")
                ReadPolicy._check_node(path)
                relative = path.relative_to(self.root).as_posix()
                if path.is_dir():
                    if relative not in directories:
                        raise ToolFailure("UNREGISTERED_CHANGE", "Workspace contains an unregistered directory")
                    walk(path)
                elif relative not in names:
                    raise ToolFailure("UNREGISTERED_CHANGE", "Workspace contains an unregistered file")

        walk(self.root)

    def capture_changes(self) -> dict[str, bytes]:
        """Validate a complete source-only candidate before displaying or exporting any bytes."""
        names = set(self.original) | set(self.policy.public_sources)
        self._check_layout(names)
        changes = {}
        for name in sorted(names):
            old = self.original.get(name)
            try:
                new, clipped = self.policy.read(name)
            except FileNotFoundError:
                raise ToolFailure("DELETE_NOT_SUPPORTED", "Deleting registered files is not supported") from None
            if clipped:
                raise ToolFailure("FILE_TOO_LARGE", "Candidate exceeds the file byte limit")
            if old == new:
                continue
            check_edit_scope(name, self.editable_globs)
            try:
                new.decode("utf-8")
            except UnicodeDecodeError:
                raise ToolFailure("NOT_TEXT", "Candidate source must be UTF-8") from None
            if b"\x00" in new:
                raise ToolFailure("NOT_TEXT", "Binary source changes are not supported")
            changes[name] = new
        if len(changes) > MAX_CANDIDATE_FILES:
            raise ToolFailure("OUTPUT_LIMIT", "Candidate exceeds the registered file-count limit")
        return changes

    def export_diff(self, destination: Path) -> None:
        changes = self.capture_changes()
        entries = []
        for name, new in sorted(changes.items()):
            old = self.original.get(name)
            entries.append(CandidateChange(path=name,
                before_sha256=hashlib.sha256(old).hexdigest() if old is not None else None,
                after_sha256=hashlib.sha256(new).hexdigest(), content=new.decode("utf-8")))
        patch = render_patch(self.original, changes)
        manifest = CandidateManifest(task_id=self.task_id, base_revision=self.base_revision,
                                     patch_sha256=hashlib.sha256(patch).hexdigest(), changes=entries,
                                     profile_sha256=self.profiles.sha256)
        destination.write_bytes(patch)
        destination.with_suffix(".manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n",
                                                            encoding="utf-8")
