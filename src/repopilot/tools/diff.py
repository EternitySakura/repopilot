"""A bounded canonical source diff against frozen bytes; never invokes git or a shell."""

from hashlib import sha256

from repopilot.contracts.action import GitDiffArgs
from repopilot.contracts.tool_data import DiffFile, GitDiffData
from repopilot.tools.errors import ToolFailure
from repopilot.workspace import Workspace, render_patch


def git_diff(workspace: Workspace, args: GitDiffArgs) -> GitDiffData:
    changes = workspace.capture_changes()
    patch = render_patch(workspace.original, changes)
    digest = sha256(patch).hexdigest()
    if args.expected_sha256 is not None and args.expected_sha256 != digest:
        raise ToolFailure("DIFF_CHANGED", "Diff changed; restart from offset 0", suggested_action="git_diff")
    if args.offset > len(patch) or (args.offset < len(patch) and patch[args.offset] & 0xC0 == 0x80):
        raise ToolFailure("INVALID_ARGUMENTS", "Offset is outside the diff or not a UTF-8 boundary")
    fragment = patch[args.offset:args.offset + args.max_bytes]
    while fragment:
        try:
            text = fragment.decode("utf-8")
            break
        except UnicodeDecodeError:
            fragment = fragment[:-1]
    else:
        if args.offset < len(patch):
            raise ToolFailure("OUTPUT_LIMIT", "Byte limit cannot fit the next UTF-8 character")
        text = ""
    end = args.offset + len(fragment)
    files = [DiffFile(path=name, status="added" if name not in workspace.original else "modified",
                      before_sha256=sha256(workspace.original[name]).hexdigest() if name in workspace.original else None,
                      after_sha256=sha256(data).hexdigest()) for name, data in sorted(changes.items())]
    return GitDiffData(base_revision=workspace.base_revision, files=files, diff=text, sha256=digest,
                       offset=args.offset, next_offset=end if end < len(patch) else None,
                       truncated=end < len(patch))
