import json
import traceback
from hashlib import sha256
from time import perf_counter
from uuid import uuid4

from pydantic import ValidationError

from repopilot.contracts.action import EDIT_ACTIONS, Action, action_name
from repopilot.contracts.tool_result import ToolError, ToolResult
from repopilot.observability.events import EventLog
from repopilot.repository.profiles import TARGET_TEST
from repopilot.tools.artifacts import ArtifactStore
from repopilot.tools.diff import git_diff
from repopilot.tools.edit import Editor
from repopilot.tools.filesystem import list_directory, read_file
from repopilot.tools.policy import ReadPolicy, ToolFailure
from repopilot.tools.registry import action_schema, enabled_tools
from repopilot.tools.search import search_code
from repopilot.tools.symbols import search_symbol
from repopilot.tools.testing import DockerTests
from repopilot.workspace import Workspace


class Dispatcher:
    def __init__(self, policy: ReadPolicy, events: EventLog, *, editor: Editor | None = None,
                 tests: DockerTests | None = None, workspace: Workspace | None = None):
        if workspace is not None and (workspace.policy is not policy
                or (editor is not None and editor.policy is not policy)
                or (tests is not None and tests.workspace is not workspace)):
            raise ValueError("Diff context must share the dispatcher policy, editor and executor workspace")
        self.policy = policy
        self.events = events
        self.editor = editor
        self.tests = tests
        self.workspace = workspace
        if tests is not None:
            if tests.workspace.policy is not policy:
                raise ValueError("Test executor must share the dispatcher policy")
            tests.executions.bind(events)
        self.enabled_tools = enabled_tools(can_edit=editor is not None, can_test=tests is not None,
                                           can_diff=workspace is not None)
        self.artifacts = ArtifactStore(events.directory, events.run_id,
                                      capture_limit=tests.test_profile.log_limit_bytes if tests is not None else TARGET_TEST.log_limit_bytes)
        # This snapshot is audit metadata, not an editable tool authorization source.
        schema = action_schema(self.enabled_tools)
        self.schema_sha256 = sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
        (events.directory / "tools.json").write_text(json.dumps({
            "registry_version": "fixture-tools-v6", "enabled_tools": self.enabled_tools,
            "schema_sha256": self.schema_sha256, "action_schema": schema,
        }, indent=2) + "\n", encoding="utf-8")

    @property
    def generation(self) -> int:
        return self.editor.generation if self.editor is not None else 0

    def dispatch(self, raw: Action | dict) -> ToolResult:
        call_id = uuid4()
        started = perf_counter()
        payload = raw.model_dump(mode="json") if isinstance(raw, Action) else raw
        kind = action_name(payload)
        self.events.write("tool.intent", tool_call_id=call_id, action_type=kind, action=payload)
        try:
            try:
                action = Action.model_validate(payload)
            except ValidationError as exc:
                code = ("PATH_DENIED" if any("relative POSIX" in e["msg"]
                        for e in exc.errors()) else "INVALID_ARGUMENTS")
                raise ToolFailure(code, str(exc)) from exc
            handlers = {"list_directory": list_directory, "read_file": read_file,
                        "search_code": search_code, "search_symbol": search_symbol}
            if action.type not in self.enabled_tools:
                raise ToolFailure("TOOL_DISABLED", "Tool is not enabled for this run")
            if action.type == "edit_file":
                data = self.editor.edit(action.arguments, call_id)
            elif action.type == "edit_files":
                data = self.editor.edit_files(action.arguments, call_id)
            elif action.type == "apply_patch":
                data = self.editor.apply_patch(action.arguments, call_id)
            elif action.type == "run_test":
                data = self.tests.run(call_id, generation=self.generation)
                try:
                    execution = self.tests.executions.get(data.execution_id)
                except ToolFailure as exc:
                    raise RuntimeError("Test result has no registered execution") from exc
                if (execution.tool_call_id != str(call_id) or execution.source_generation != self.generation
                        or execution.state != "completed" or execution.cleanup_status != "confirmed"
                        or execution.input_manifest_sha256 != data.input_manifest_sha256
                        or data.source_generation != execution.source_generation
                        or data.cleanup_status != execution.cleanup_status
                        or any(getattr(execution, key) != getattr(data, key)
                               for key in ("exit_code", "passed", "failed", "public_tests_passed"))):
                    raise RuntimeError("Test result is not bound to a confirmed execution")
                current = self.tests.status(data.execution_id, generation=self.generation)
                data = data.model_copy(update={"stale": current.stale})
                record = self.artifacts.register_test_log(call_id, log_ref=data.log_ref,
                    generation=self.generation, source_truncated=data.truncated,
                    execution_id=data.execution_id, input_manifest_sha256=data.input_manifest_sha256)
                self.tests.executions.attach_artifact(data.execution_id, record.artifact_id)
                data = data.model_copy(update={"artifact_id": record.artifact_id})
                self.events.write("artifact.registered", tool_call_id=call_id, artifact_id=record.artifact_id,
                                  visibility="agent", kind="public_test_log", source_generation=self.generation,
                                  execution_id=data.execution_id, artifact_record=record.model_dump())
            elif action.type == "get_execution_status":
                data = self.tests.status(action.arguments.execution_id, generation=self.generation)
            elif action.type == "read_test_result":
                data = self.artifacts.read_test_result(action.arguments, generation=self.generation)
                if data.execution_id is not None:
                    execution = self.tests.status(data.execution_id, generation=self.generation)
                    data = data.model_copy(update={"stale": data.stale or execution.stale})
            elif action.type == "git_diff":
                data = git_diff(self.workspace, action.arguments)
            else:
                data = handlers[action.type](self.policy, action.arguments)
            result = ToolResult(
                tool_call_id=call_id, ok=True, data=data.model_dump(),
                truncated=data.truncated, workspace_generation=self.generation,
                artifact_ref=data.log_ref if action.type == "run_test" else None,
                duration_ms=int((perf_counter() - started) * 1000),
            )
        except (ToolFailure, FileNotFoundError,
                NotADirectoryError, PermissionError) as exc:
            if isinstance(exc, ToolFailure):
                code = exc.code
            elif isinstance(exc, FileNotFoundError):
                code = "NOT_FOUND"
            elif isinstance(exc, NotADirectoryError):
                code = "NOT_DIRECTORY"
            else:
                code = "PATH_DENIED"
            result = ToolResult(
                tool_call_id=call_id, ok=False,
                data={"execution_id": exc.execution_id} if getattr(exc, "execution_id", None) else {},
                error=ToolError(code=code, message=str(exc),
                                suggested_action=getattr(exc, "suggested_action", None)),
                artifact_ref=getattr(exc, "artifact_ref", None), workspace_generation=self.generation,
                duration_ms=int((perf_counter() - started) * 1000),
            )
        except KeyboardInterrupt as exc:
            self.events.write("tool.failed", tool_call_id=call_id, action_type=kind, code="USER_CANCELLED",
                              outcome_unknown=kind in EDIT_ACTIONS, execution_id=getattr(exc, "execution_id", None))
            raise
        except Exception as exc:
            self.events.write("tool.failed", tool_call_id=call_id, action_type=kind,
                              diagnostic=traceback.format_exc(), fatal=True,
                              execution_id=getattr(exc, "execution_id", None))
            raise
        self.events.write("tool.completed" if result.ok else "tool.failed",
                          tool_call_id=call_id, action_type=kind,
                          result=result.model_dump(mode="json"))
        return result
