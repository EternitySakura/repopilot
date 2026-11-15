"""Apply a single-file unified diff to in-memory UTF-8 bytes, with exact contexts."""

import re

from repopilot.tools.errors import ToolFailure

HUNK = re.compile(r"@@ -(\d{1,6})(?:,(\d{1,6}))? \+(\d{1,6})(?:,(\d{1,6}))? @@(?: .*)?")
NO_NEWLINE = "\\ No newline at end of file\n"


def invalid(message: str):
    raise ToolFailure("PATCH_INVALID", message)


def conflict():
    raise ToolFailure("PATCH_CONTEXT_CONFLICT", "Patch positions or context differ from the current file",
                      suggested_action="read_file")


def apply_unified_patch(original: bytes, patch: str, path: str) -> bytes:
    try:
        source = original.decode("utf-8").splitlines(keepends=True)
    except UnicodeError as exc:
        raise ToolFailure("NOT_TEXT", "Patch editing requires UTF-8 source") from exc
    lines = patch.splitlines(keepends=True)
    index = 0
    if lines and lines[0].startswith("diff --git "):
        if lines[0] != f"diff --git a/{path} b/{path}\n":
            raise ToolFailure("PATCH_PATH_MISMATCH", "Patch paths must match the action path")
        index += 1
        if index < len(lines) and lines[index].startswith("index "):
            index += 1
    if lines[index:index + 2] != [f"--- a/{path}\n", f"+++ b/{path}\n"]:
        raise ToolFailure("PATCH_PATH_MISMATCH", "Expected matching single-file a/ and b/ patch headers")
    index += 2
    cursor = 0
    output = []
    hunks = 0
    while index < len(lines):
        header = HUNK.fullmatch(lines[index].rstrip("\r\n"))
        if header is None:
            invalid("Expected a unified hunk; extra files and patch extensions are not supported")
        old_start, old_count, new_start, new_count = (int(value) if value is not None else 1 for value in header.groups())
        if (old_count and old_start == 0) or (new_count and new_start == 0):
            invalid("Nonempty hunk ranges start at line 1")
        old_pos = old_start - 1 if old_count else old_start
        new_pos = new_start - 1 if new_count else new_start
        if old_pos < cursor or old_pos > len(source) or old_pos + old_count > len(source):
            conflict()
        output.extend(source[cursor:old_pos])
        if new_pos != len(output):
            conflict()
        index += 1
        consumed_old = consumed_new = 0
        while consumed_old < old_count or consumed_new < new_count:
            if index >= len(lines) or lines[index][0] not in " +-" or not lines[index].endswith("\n"):
                invalid("Incomplete hunk or invalid line prefix")
            tag, content = lines[index][0], lines[index][1:]
            index += 1
            if index < len(lines) and lines[index] == NO_NEWLINE:
                content = content[:-1]
                index += 1
            if tag in " -":
                if consumed_old >= old_count or source[old_pos + consumed_old] != content:
                    conflict()
                consumed_old += 1
            if tag in " +":
                output.append(content)
                consumed_new += 1
            if consumed_new > new_count:
                invalid("Hunk line counts differ from its header")
        cursor = old_pos + old_count
        hunks += 1
    if not hunks:
        invalid("Patch must contain a unified hunk")
    output.extend(source[cursor:])
    updated = "".join(output)
    if updated.splitlines(keepends=True) != output:
        invalid("A missing-newline marker may only terminate the resulting file")
    try:
        return updated.encode("utf-8")
    except UnicodeError as exc:
        raise ToolFailure("NOT_TEXT", "Patch must contain valid UTF-8 text") from exc
