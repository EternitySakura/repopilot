from repopilot.contracts.action import SearchCodeArgs
from repopilot.contracts.tool_data import SearchCodeData, SearchMatch
from repopilot.tools.policy import ReadPolicy, ToolFailure


class SearchScan:
    """Shared public-tree traversal and byte budget for literal and symbol search."""

    def __init__(self, policy: ReadPolicy):
        self.policy = policy
        self.visited = 0
        self.bytes_read = 0
        self.truncated = False

    def paths(self, path: str, depth: int = 1):
        for name, child in self.policy.children(path):
            self.visited += 1
            if self.visited > self.policy.profile.max_search_entries:
                self.truncated = True
                return
            if child.is_dir():
                if depth < self.policy.profile.max_directory_depth:
                    yield from self.paths(name, depth + 1)
                else:
                    self.truncated = True
            else:
                yield name

    def read(self, path: str) -> tuple[bytes, bool] | None:
        if self.bytes_read + self.policy.max_bytes > self.policy.profile.max_search_bytes:
            self.truncated = True
            return None
        content, clipped = self.policy.read(path)
        self.bytes_read += len(content)
        self.truncated |= clipped
        return content, clipped


def search_code(policy: ReadPolicy, args: SearchCodeArgs) -> SearchCodeData:
    if args.limit > policy.profile.max_search_matches:
        raise ToolFailure("INVALID_ARGUMENTS", "Search request exceeds the repository profile limit")
    directory = policy.check(args.path)
    if not directory.is_dir():
        raise ToolFailure("NOT_DIRECTORY", "Expected a search directory")
    matches = []
    scan = SearchScan(policy)
    for name in scan.paths(args.path):
        read = scan.read(name)
        if read is None:
            break
        content, _ = read
        for line, text in enumerate(content.decode("utf-8", errors="replace").splitlines(), 1):
            if args.query in text:
                if len(matches) == args.limit:
                    return SearchCodeData(matches=matches, truncated=True)
                matches.append(SearchMatch(path=name, line=line, text=text))
    return SearchCodeData(matches=matches, truncated=scan.truncated)
