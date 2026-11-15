import hashlib

from repopilot.contracts.action import ListDirectoryArgs, ReadFileArgs
from repopilot.contracts.tool_data import (
    DirectoryEntry,
    FileLine,
    ListDirectoryData,
    ReadFileData,
)
from repopilot.tools.policy import ReadPolicy, ToolFailure


def list_directory(policy: ReadPolicy, args: ListDirectoryArgs) -> ListDirectoryData:
    if args.depth > policy.profile.max_directory_depth or args.limit > policy.profile.max_directory_entries:
        raise ToolFailure("INVALID_ARGUMENTS", "Directory request exceeds the repository profile limits")
    entries = []

    def walk(path: str, depth: int):
        for name, child in policy.children(path):
            yield DirectoryEntry(path=name, type="directory" if child.is_dir() else "file")
            if child.is_dir() and depth < args.depth:
                yield from walk(name, depth + 1)

    for entry in walk(args.path, 1):
        if len(entries) == args.limit:
            return ListDirectoryData(entries=entries, truncated=True)
        entries.append(entry)
    return ListDirectoryData(entries=entries)


def read_file(policy: ReadPolicy, args: ReadFileArgs) -> ReadFileData:
    content, clipped = policy.read(args.path)
    lines = content.decode("utf-8", errors="replace").splitlines()
    return ReadFileData(
        lines=[FileLine(line=i, text=text) for i, text in enumerate(lines, 1)
               if args.start_line <= i <= args.end_line],
        sha256=None if clipped else hashlib.sha256(content).hexdigest(),
        truncated=clipped or len(lines) > args.end_line,
    )
