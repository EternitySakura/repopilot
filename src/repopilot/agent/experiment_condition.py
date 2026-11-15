"""Controller policies shared by frozen comparison plans and development runs."""

import re
from dataclasses import asdict, dataclass, replace
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from types import MappingProxyType

from repopilot.contracts.action import EDIT_ACTIONS, SearchSymbolArgs
from repopilot.tools.symbols import search_symbol

CONDITION_VERSION = "comparison-v1"
FEEDBACK_LIMIT = 4
RETRIEVAL_QUERIES = 4
RETRIEVAL_MATCHES = 6
GENERATION_BOUND = {"list_directory", "read_file", "search_code", "search_symbol", "git_diff",
                    "run_test", "read_test_result", "get_execution_status"}


@dataclass(frozen=True)
class ExperimentCondition:
    name: str
    proactive_retrieval: bool
    structured_plan: bool
    structured_test_feedback: bool
    feedback_order: str

    def snapshot(self) -> dict:
        return {"version": CONDITION_VERSION, **asdict(self), "feedback_limit": FEEDBACK_LIMIT,
                "retrieval_query_limit": RETRIEVAL_QUERIES if self.proactive_retrieval else 0,
                "retrieval_match_limit": RETRIEVAL_MATCHES if self.proactive_retrieval else 0}


CONDITIONS = MappingProxyType({
    "B0": ExperimentCondition("B0", False, False, False, "recent"),
    "B1": ExperimentCondition("B1", True, True, True, "test_and_source_priority"),
})

DIAGNOSTIC_CONDITIONS = MappingProxyType({
    "B1": CONDITIONS["B1"],
    "B1-no-retrieval": replace(CONDITIONS["B1"], name="B1-no-retrieval", proactive_retrieval=False),
    "B1-recent-context": replace(CONDITIONS["B1"], name="B1-recent-context", feedback_order="recent"),
})


def retrieve_public_leads(policy, description: str, editable_globs) -> dict:
    paths = sorted(path for path in policy.profile.public_files if path.endswith(".py")
                   and any(fnmatchcase(path, pattern) for pattern in editable_globs))[:RETRIEVAL_MATCHES]
    names = re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,63}", description)
    queries = dict.fromkeys([*names, *(PurePosixPath(path).stem for path in paths)])
    matches = []
    leads = {"source_paths": paths, "symbol_locations": matches}
    limit = min(RETRIEVAL_MATCHES, policy.profile.max_search_matches)
    for query in list(queries)[:RETRIEVAL_QUERIES]:
        result = search_symbol(policy, SearchSymbolArgs(query=query, path="src", limit=limit))
        for symbol in result.symbols:
            item = {"path": symbol.path, "name": symbol.qualified_name,
                    "start_line": symbol.line, "end_line": symbol.end_line}
            if item not in matches:
                matches.append(item)
            if len(matches) == RETRIEVAL_MATCHES:
                return leads
    return leads


def _action_type(action):
    value = action.get("type")
    return value if isinstance(value, str) else None


def retained_feedback(condition: ExperimentCondition, actions, observations) -> set[int]:
    generation = observations[-1].workspace_generation if observations else 0
    choices = [index for index, (action, result) in enumerate(zip(actions, observations, strict=False))
               if _action_type(action) not in GENERATION_BOUND or result.workspace_generation == generation]
    if condition.feedback_order == "recent":
        return set(choices[-FEEDBACK_LIMIT:])

    def priority(index):
        action = _action_type(actions[index])
        if action in {"run_test", "read_test_result"}:
            return 0
        if not observations[index].ok:
            return 1
        if action in {"read_file", "search_symbol", "git_diff"}:
            return 2
        return 3

    return set(sorted(choices, key=lambda index: (priority(index), -index))[:FEEDBACK_LIMIT])


def structured_context(condition: ExperimentCondition, actions, observations, leads) -> dict:
    generation = observations[-1].workspace_generation if observations else 0
    history = [(_action_type(action), result) for action, result in zip(actions, observations, strict=False)]
    test = next((result for action, result in reversed(history)
                 if action == "run_test" and result.workspace_generation == generation), None)
    feedback = None
    if test is not None:
        feedback = ({"status": "passed" if test.data.get("public_tests_passed") else "failed",
                     **{key: test.data.get(key) for key in ("exit_code", "passed", "failed", "artifact_id", "execution_id")}}
                    if test.ok else {"status": "error", "code": test.error.code})
    edited = any(action in EDIT_ACTIONS and result.ok for action, result in history)
    inspected = any(action in {"read_file", "search_symbol"} and result.ok for action, result in history)
    reviewed = any(action == "git_diff" and result.ok and result.workspace_generation == generation
                   and not result.truncated for action, result in history)
    read_failure = feedback is not None and feedback.get("artifact_id") is not None and any(
        action == "read_test_result" and result.ok and result.workspace_generation == generation
        and result.data.get("artifact_id") == feedback["artifact_id"] for action, result in history)
    if feedback is not None and feedback["status"] == "passed" and edited:
        step = "finish" if reviewed else "review_diff"
    elif feedback is not None and feedback["status"] != "passed" and (edited or not inspected):
        step = "revise_scoped_edit" if read_failure else "inspect_test_failure"
    elif edited:
        step = "run_public_tests"
    elif inspected:
        step = "prepare_scoped_edit"
    else:
        step = "inspect_relevant_source"
    context = {}
    if condition.structured_plan:
        context["plan"] = {"next_step": step, "workspace_generation": generation}
    if condition.structured_test_feedback:
        context["public_test_feedback"] = feedback
    if condition.proactive_retrieval and generation == 0:
        context["public_source_leads"] = leads
    return context
