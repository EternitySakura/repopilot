"""Trusted declarations shared by advertised schemas and runtime capability checks."""

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from repopilot.contracts.action import Action


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    capability: Literal["repository", "edit", "test", "workspace"]


TOOLS = MappingProxyType({tool.name: tool for tool in (
    ToolDefinition("list_directory", "repository"), ToolDefinition("search_code", "repository"),
    ToolDefinition("read_file", "repository"), ToolDefinition("edit_file", "edit"),
    ToolDefinition("run_test", "test"), ToolDefinition("read_test_result", "test"),
    ToolDefinition("git_diff", "workspace"),
    ToolDefinition("get_execution_status", "test"),
    ToolDefinition("search_symbol", "repository"),
    ToolDefinition("apply_patch", "edit"),
    ToolDefinition("edit_files", "edit"),
)})


def enabled_tools(*, can_edit: bool = False, can_test: bool = False, can_diff: bool = False) -> tuple[str, ...]:
    capabilities = {"repository"}
    if can_edit:
        capabilities.add("edit")
    if can_test:
        capabilities.add("test")
    if can_diff:
        capabilities.add("workspace")
    return tuple(name for name, tool in TOOLS.items() if tool.capability in capabilities)


def action_schema(names: tuple[str, ...]) -> dict:
    if not names or len(names) != len(set(names)) or set(names) - TOOLS.keys():
        raise ValueError("Unknown, duplicate or empty enabled tool set")
    schema = copy.deepcopy(Action.model_json_schema())
    mapping = schema["discriminator"]["mapping"]
    schema["discriminator"]["mapping"] = {name: mapping[name] for name in names}
    refs = set(schema["discriminator"]["mapping"].values())
    schema["oneOf"] = [item for item in schema["oneOf"] if item["$ref"] in refs]
    definitions = schema.pop("$defs")
    reachable = {}

    def collect(value):
        if isinstance(value, dict):
            reference = value.get("$ref", "")
            if reference.startswith("#/$defs/"):
                key = reference.removeprefix("#/$defs/")
                if key not in reachable:
                    reachable[key] = definitions[key]
                    collect(definitions[key])
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(schema)
    schema["$defs"] = reachable
    return schema
