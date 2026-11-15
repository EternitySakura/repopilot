"""Exact edits and reversible change sets in controller-created candidate copies."""

import hashlib
import json
import os
import tempfile
import traceback
from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from repopilot.contracts.action import ApplyPatchArgs, EditFileArgs, EditFilesArgs
from repopilot.contracts.tool_data import EditFileData, EditFilesData
from repopilot.observability.events import EventLog
from repopilot.tools.patches import apply_unified_patch
from repopilot.tools.policy import ReadPolicy, ToolFailure


def check_edit_scope(relative: str, editable_globs: Sequence[str]) -> None:
    parts = PurePosixPath(relative).parts
    matches = any(len(parts) == len(PurePosixPath(pattern).parts)
                  and all(fnmatchcase(part, glob) for part, glob in
                          zip(parts, PurePosixPath(pattern).parts, strict=True))
                  for pattern in editable_globs)
    if not parts or parts[0] != "src" or not relative.endswith(".py") or not matches:
        raise ToolFailure("EDIT_DENIED", "Only task-authorized Python source files can be edited")
    if any(part.lower() == "conftest.py" for part in parts):
        raise ToolFailure("EDIT_DENIED", "Pytest hooks cannot be edited or created")


class Editor:
    def __init__(self, policy: ReadPolicy, editable_globs: list[str], events: EventLog):
        self.policy = policy
        self.editable_globs = tuple(editable_globs)
        self.events = events
        self.generation = 0

    def check_scope(self, relative: str) -> None:
        check_edit_scope(relative, self.editable_globs)

    def edit(self, args: EditFileArgs, call_id) -> EditFileData:
        original, updated = self._prepare(args)
        return self._commit(args.path, original, updated, call_id)

    def _prepare(self, args: EditFileArgs) -> tuple[bytes | None, bytes]:
        self.check_scope(args.path)
        if args.mode == "create":
            path = self.policy.check(args.path, additional_public_files=frozenset({args.path}), allow_missing_leaf=True)
            if path.exists():
                raise ToolFailure("ALREADY_EXISTS", "create never overwrites an existing file")
            original = None
            updated = args.new_text.encode("utf-8")
        else:
            original = self._read_existing(args.path, args.expected_sha256)
            try:
                text = original.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolFailure("NOT_TEXT", "Editing requires UTF-8 text") from exc
            if text.count(args.old_text) != 1:
                raise ToolFailure("MATCH_CONFLICT", "old_text must match exactly once",
                                  suggested_action="read_file")
            updated = text.replace(args.old_text, args.new_text, 1).encode("utf-8")
        if len(updated) > self.policy.max_bytes:
            raise ToolFailure("FILE_TOO_LARGE", "Edited file exceeds byte limit")
        return original, updated

    def edit_files(self, args: EditFilesArgs, call_id) -> EditFilesData:
        # Every path, expected hash, exact replacement and byte limit is checked before any write.
        prepared = [(item.path, *self._prepare(item)) for item in args.files]
        files = [EditFileData(path=name,
                  before_sha256=hashlib.sha256(old).hexdigest() if old is not None else None,
                  after_sha256=hashlib.sha256(new).hexdigest()) for name, old, new in prepared]

        def manifest_hash(field):
            return hashlib.sha256(json.dumps(sorted((item.path, getattr(item, field)) for item in files)).encode()).hexdigest()

        result = EditFilesData(files=files, changed_files=sum(old != new for _, old, new in prepared),
                               before_sha256=manifest_hash("before_sha256"), after_sha256=manifest_hash("after_sha256"))
        if not result.changed_files:
            return result
        artifact = self.events.directory / "edits" / str(call_id)
        artifact.mkdir(parents=True, exist_ok=False)
        journal = {"version": 1, "tool_call_id": str(call_id), "status": "prepared",
                   "generation_before": self.generation, "generation_after": self.generation, "files": []}
        for index, ((_, old, new), data) in enumerate(zip(prepared, files, strict=True)):
            folder = artifact / str(index)
            folder.mkdir()
            (folder / "after.bin").write_bytes(new)
            if old is not None:
                (folder / "before.bin").write_bytes(old)
            journal["files"].append({"index": index, **data.model_dump(exclude={"truncated"})})

        def save():
            raw = (json.dumps(journal, indent=2) + "\n").encode()
            temporary = artifact / "journal.tmp"
            with temporary.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, artifact / "journal.json")
            return hashlib.sha256(raw).hexdigest()

        save()
        self.events.write("edit.batch_started", tool_call_id=call_id, action_type="edit_files",
                          files=journal["files"], workspace_generation=self.generation)
        attempted = []
        try:
            for name, old, new in prepared:
                if old == new:
                    continue
                attempted.append((name, old, new))
                self._write_change(name, old, new)
        except BaseException as cause:
            rollback_errors = []
            for name, old, new in reversed(attempted):
                try:
                    self._restore_change(name, old, new)
                except BaseException as rollback_error:  # noqa: BLE001 - finish reverse rollback before reporting interruption
                    rollback_errors.append({"type": type(rollback_error).__name__, "diagnostic": traceback.format_exc()})
            journal["status"] = "rollback_failed" if rollback_errors else "rolled_back"
            journal["rollback_errors"] = rollback_errors
            if rollback_errors:
                # Invalidate old tests if the workspace could retain any partial change.
                self.generation += 1
            journal["generation_after"] = self.generation
            checksum = save()
            self.events.write("edit.batch_finished", tool_call_id=call_id, action_type="edit_files",
                              status=journal["status"], journal_sha256=checksum, workspace_generation=self.generation)
            if rollback_errors:
                raise RuntimeError("Batch rollback incomplete; inspect the saved change set") from cause
            if isinstance(cause, OSError):
                # A write failure is a program/environment failure, not a recoverable model parameter error.
                raise RuntimeError("Batch write failed; prior changes were rolled back") from cause  # noqa: TRY004 - fatal I/O failure
            raise
        self.policy.public_sources |= {name for name, _, _ in prepared}
        self.generation += 1
        journal.update(status="committed", generation_after=self.generation)
        checksum = save()
        self.events.write("edit.batch_finished", tool_call_id=call_id, action_type="edit_files",
                          status="committed", journal_sha256=checksum, workspace_generation=self.generation)
        return result.model_copy(update={"transaction_id": str(call_id)})

    def _restore_change(self, relative: str, original: bytes | None, updated: bytes):
        try:
            path = self.policy.check(relative, additional_public_files=frozenset({relative}))
            with path.open("rb") as stream:
                current = stream.read(self.policy.max_bytes + 1)
        except FileNotFoundError:
            if original is None:
                return
            raise
        if current == original:
            return
        if current != updated:
            raise RuntimeError("File changed outside the batch; rollback stopped")
        if original is None:
            path.unlink()
        else:
            self._write_change(relative, updated, original)

    def apply_patch(self, args: ApplyPatchArgs, call_id) -> EditFileData:
        self.check_scope(args.path)
        original = self._read_existing(args.path, args.expected_sha256)
        updated = apply_unified_patch(original, args.patch, args.path)
        return self._commit(args.path, original, updated, call_id, action_type="apply_patch")

    def _read_existing(self, relative: str, expected_sha256: str) -> bytes:
        path = self.policy.check(relative, additional_public_files=frozenset({relative}))
        if not path.is_file():
            raise ToolFailure("NOT_FILE", "Expected a regular file")
        with path.open("rb") as stream:
            original = stream.read(self.policy.max_bytes + 1)
        if len(original) > self.policy.max_bytes:
            raise ToolFailure("FILE_TOO_LARGE", "File exceeds edit byte limit")
        if hashlib.sha256(original).hexdigest() != expected_sha256:
            raise ToolFailure("HASH_CONFLICT", "File changed; read it again before editing", suggested_action="read_file")
        return original

    def _commit(self, relative: str, original: bytes | None, updated: bytes, call_id, *, action_type="edit_file") -> EditFileData:
        self.policy.check(relative, additional_public_files=frozenset({relative}), allow_missing_leaf=original is None)
        before = hashlib.sha256(original).hexdigest() if original is not None else None
        if len(updated) > self.policy.max_bytes:
            raise ToolFailure("FILE_TOO_LARGE", "Edited file exceeds byte limit")
        after = hashlib.sha256(updated).hexdigest()
        if before == after:
            return EditFileData(path=relative, before_sha256=before, after_sha256=after)
        artifact = self.events.directory / "edits" / str(call_id)
        artifact.mkdir(parents=True, exist_ok=False)
        # Save exact bytes and hashes before the write; these are trace artifacts, not recovery.
        (artifact / "after.bin").write_bytes(updated)
        if before is not None:
            (artifact / "before.bin").write_bytes(original)
        self.events.write("edit.intent", tool_call_id=call_id, action_type=action_type,
                          path=relative, before_sha256=before, after_sha256=after,
                          workspace_generation=self.generation, artifact_ref=str(artifact))
        self._write_change(relative, original, updated)
        self.policy.public_sources |= {relative}
        self.generation += 1
        return EditFileData(path=relative, before_sha256=before, after_sha256=after)

    def _write_change(self, relative: str, original: bytes | None, updated: bytes):
        path = self.policy.check(relative, additional_public_files=frozenset({relative}), allow_missing_leaf=original is None)
        if original is None:
            with path.open("xb") as stream:
                stream.write(updated)
        else:
            # Atomic single-file primitive; the change-set caller coordinates rollback.
            descriptor, temporary = tempfile.mkstemp(prefix=".repopilot-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(updated)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.policy.check(relative, additional_public_files=frozenset({relative}))
                with path.open("rb") as stream:
                    current = stream.read(self.policy.max_bytes + 1)
                if current != original:
                    raise ToolFailure("HASH_CONFLICT", "File changed during edit",
                                      suggested_action="read_file")
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
