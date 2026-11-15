"""Controller-owned read policy; edit globs never grant read access."""

import stat
from pathlib import Path, PurePosixPath, PureWindowsPath

from repopilot.repository.profiles import FIXTURE_REPO, RepoProfile
from repopilot.tools.errors import ToolFailure


class ReadPolicy:
    def __init__(self, root: Path, *, max_bytes: int | None = None,
                 public_tests: frozenset[str] | None = None,
                 public_sources: frozenset[str] | None = None, profile: RepoProfile = FIXTURE_REPO):
        self.profile = profile
        max_bytes = max_bytes if max_bytes is not None else profile.max_file_bytes
        if not 1 <= max_bytes <= profile.max_file_bytes:
            raise ValueError("max_bytes must be positive and within the repository profile cap")
        # Do not resolve away a junction before checking it.
        self.root = root.absolute()
        self.max_bytes = max_bytes
        self.public_tests = public_tests if public_tests is not None else frozenset(profile.public_tests)
        self.public_sources = public_sources if public_sources is not None else frozenset(profile.public_sources)
        for parent in (*reversed(self.root.parents), self.root):
            self._check_node(parent)

    @staticmethod
    def _check_node(path: Path) -> None:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ToolFailure("PATH_DENIED", "Links and reparse points are forbidden")

    def check(self, relative: str, *, additional_public_files: frozenset[str] = frozenset(),
              allow_missing_leaf: bool = False) -> Path:
        parts = PurePosixPath(relative).parts
        if (not relative or PurePosixPath(relative).is_absolute()
                or PureWindowsPath(relative).drive or "\\" in relative
                or ":" in relative or "\x00" in relative or ".." in parts
                or any(PureWindowsPath(part).is_reserved() or part.rstrip(" .") != part for part in parts)):
            raise ToolFailure("PATH_DENIED", "Expected a repository-relative POSIX path")
        # An allowlist keeps verifier files outside the readable workspace.
        if any(p.startswith(".") or p.lower() in {
            "__pycache__", "node_modules", "build", "dist", "private", "hidden",
            "verifier", "hidden_tests", "private_tests",
        } or p.lower().endswith((".pyc", ".pyo")) for p in parts):
            raise ToolFailure("PATH_DENIED", "Path is outside the public read scope")
        public_files = self.public_sources | self.public_tests | additional_public_files | frozenset(self.profile.public_config)
        allowed = (not parts or relative in public_files
                   or any(name.startswith(relative + "/") for name in public_files))
        if not allowed:
            raise ToolFailure("PATH_DENIED", "Path is outside the public read scope")
        candidate = self.root
        self._check_node(candidate)
        for index, part in enumerate(parts):
            candidate = candidate / part
            try:
                self._check_node(candidate)
            except FileNotFoundError:
                if not (allow_missing_leaf and index == len(parts) - 1):
                    raise
                # Only a missing final component is allowed; parents were checked above.
                return candidate
        if not candidate.resolve().is_relative_to(self.root.resolve()):
            raise ToolFailure("PATH_DENIED", "Path escapes repository")
        info = candidate.stat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ToolFailure("PATH_DENIED", "Only regular files and directories are readable")
        return candidate

    def children(self, relative: str):
        directory = self.check(relative)
        if not directory.is_dir():
            raise ToolFailure("NOT_DIRECTORY", "Expected a directory")
        for child in sorted(directory.iterdir(), key=lambda p: p.name):
            name = child.relative_to(self.root).as_posix()
            try:
                yield name, self.check(name)
            except ToolFailure as exc:
                if exc.code != "PATH_DENIED":
                    raise

    def read(self, relative: str) -> tuple[bytes, bool]:
        path = self.check(relative)
        if not path.is_file():
            raise ToolFailure("NOT_FILE", "Expected a regular file")
        with path.open("rb") as stream:
            content = stream.read(self.max_bytes)
            truncated = stream.read(1) != b""
        return content, truncated
