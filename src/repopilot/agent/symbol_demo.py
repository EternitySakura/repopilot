"""Scripted symbol selection followed by a real definition read; no target execution."""

from repopilot.agent.demo import run_demo
from repopilot.contracts.action import SearchSymbolArgs
from repopilot.repository.tasks import DEFAULT_TASK_ID


class SymbolFakeModel:
    def __init__(self, arguments: SearchSymbolArgs | None = None, *, read_definition: bool = True):
        self.arguments = arguments if arguments is not None else SearchSymbolArgs(query="parse_words", path="src")
        self.read_definition = read_definition

    def decide(self, observations):
        if not observations:
            return {"type": "search_symbol", "arguments": self.arguments.model_dump()}
        if self.read_definition and len(observations) == 1 and observations[0].ok and observations[0].data["symbols"]:
            symbol = observations[0].data["symbols"][0]
            return {"type": "read_file", "arguments": {
                "path": symbol["path"], "start_line": symbol["line"], "end_line": symbol["end_line"],
            }}
        return None


def run_symbol_demo(runs=None, *, settings=None, arguments: SearchSymbolArgs | None = None,
                    task_id: str = DEFAULT_TASK_ID, read_definition: bool = True):
    return run_demo(runs, model=SymbolFakeModel(arguments, read_definition=read_definition),
                    settings=settings, task_id=task_id)
