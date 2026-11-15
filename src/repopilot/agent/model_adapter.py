from collections.abc import Sequence
from itertools import cycle
from typing import Any, Protocol

from repopilot.contracts.action import Action
from repopilot.contracts.tool_result import ToolResult

Decision = Action | dict[str, Any]


class ModelAdapter(Protocol):
    def decide(self, observations: Sequence[ToolResult]) -> Decision | None: ...


class FakeModel:
    """Only action selection is scripted; observations come from real tools."""

    def __init__(self, actions: Sequence[Decision], *, repeat: bool = False):
        # Raw structured decisions are validated by Dispatcher at the trust boundary.
        self.actions = cycle(actions) if repeat else iter(actions)

    def decide(self, observations: Sequence[ToolResult]) -> Decision | None:
        return next(self.actions, None)


def demo_model(scenario: str = "normal") -> FakeModel:
    actions = [
        {"type": "list_directory", "arguments": {"path": "."}},
        {"type": "search_code", "arguments": {"query": "parse", "path": "src"}},
        {"type": "read_file", "arguments": {"path": "src/fixture_text/parser.py"}},
    ]
    if scenario == "invalid":
        actions.insert(0, {"type": "read_file", "arguments": {
            "path": "src/fixture_text/parser.py", "query": "undeclared",
        }})
    elif scenario not in {"normal", "repeat"}:
        raise ValueError(f"Unknown demo scenario: {scenario}")
    return FakeModel(actions, repeat=scenario == "repeat")
