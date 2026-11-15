"""Versioned controller registrations, never loaded from model-selected files or URLs."""

import hashlib
import json
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from pydantic import ConfigDict, Field

from repopilot.contracts.base import Contract
from repopilot.contracts.task import TaskSpec
from repopilot.tools.errors import ToolFailure


class FrozenProfile(Contract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: str
    version: int = Field(ge=1)


class RepoProfile(FrozenProfile):
    fixture_directory: str
    base_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    public_sources: tuple[str, ...]
    public_tests: tuple[str, ...]
    public_config: tuple[str, ...]
    editable_globs: tuple[str, ...]
    max_file_bytes: int = Field(ge=1)
    max_directory_depth: int = Field(ge=1)
    max_directory_entries: int = Field(ge=1)
    max_search_matches: int = Field(ge=1)
    max_search_entries: int = Field(ge=1)
    max_search_bytes: int = Field(ge=1)

    @property
    def public_files(self) -> frozenset[str]:
        return frozenset(self.public_sources + self.public_tests + self.public_config)


class TestProfile(FrozenProfile):
    repo_name: str
    test_paths: tuple[str, ...]
    expected_nodes: tuple[str, ...]
    memory_mib: int = Field(ge=1)
    cpus: int = Field(ge=1)
    pids_limit: int = Field(ge=1)
    tmp_mib: int = Field(ge=1)
    log_limit_bytes: int = Field(ge=1)

    def pytest_args(self, *, gate_a: bool = False) -> tuple[str, ...]:
        common = ("-q", "-p", "no:cacheprovider")
        if gate_a:
            common += ("--noconftest", "-c", "/fixture/pyproject.toml", "-o", "addopts=")
        return common + self.test_paths

    def container_args(self) -> list[str]:
        return ["--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "65534:65534",
                "--memory", f"{self.memory_mib}m", "--cpus", str(self.cpus),
                "--pids-limit", str(self.pids_limit), "--tmpfs",
                f"/tmp:rw,noexec,nosuid,size={self.tmp_mib}m"]


class BudgetProfile(FrozenProfile):
    max_iterations: int = Field(ge=1)
    max_tool_calls: int = Field(ge=1)
    max_edit_calls: int = Field(ge=1)
    max_test_calls: int = Field(ge=1)
    max_no_progress: int = Field(ge=1)
    tool_timeout_seconds: int = Field(ge=1)

    def check_settings(self, settings) -> None:
        for name in type(self).model_fields:
            if name not in {"name", "version"} and getattr(settings, name) > getattr(self, name):
                raise ToolFailure("BUDGET_PROFILE_DENIED", f"{name} exceeds the registered profile cap")


FIXTURE_REPO = RepoProfile(
    name="fixture_text", version=1, fixture_directory="fixtures/fixture_text",
    base_revision="02c05e04652b8029da279e6f6de9f5ee1055e931151215fe80b408ab509f9f57",
    image_id="sha256:a5901493e7d36bd81679d2cd7c5e0d1117a7adedf8165194c9e165ca5003224f",
    public_sources=("src/fixture_text/__init__.py", "src/fixture_text/parser.py"),
    public_tests=("tests/test_parser.py",),
    public_config=("Dockerfile", "pyproject.toml", "requirements-test.txt"),
    editable_globs=("src/fixture_text/*.py",), max_file_bytes=65536,
    max_directory_depth=3, max_directory_entries=500, max_search_matches=100,
    max_search_entries=2000, max_search_bytes=4 * 1024 * 1024,
)
TARGET_TEST = TestProfile(
    name="target", version=1, repo_name="fixture_text", test_paths=("tests/test_parser.py",),
    expected_nodes=("tests/test_parser.py::test_comma_separated_words",
                    "tests/test_parser.py::test_whitespace_and_blank_fields",
                    "tests/test_parser.py::test_empty_input"),
    memory_mib=256, cpus=1, pids_limit=64, tmp_mib=64, log_limit_bytes=1024 * 1024,
)
LOCAL_BUDGET = BudgetProfile(name="local_mvp", version=1, max_iterations=30, max_tool_calls=60,
                            max_edit_calls=5, max_test_calls=8, max_no_progress=3, tool_timeout_seconds=60)
def _development_profiles(name, module, revision, nodes, extra_sources=()):
    repo = FIXTURE_REPO.model_copy(update={
        "name": name, "fixture_directory": f"fixtures/{name}", "base_revision": revision,
        "public_sources": (f"src/{name}/__init__.py", f"src/{name}/{module}.py") + tuple(f"src/{name}/{source}" for source in extra_sources),
        "public_tests": (f"tests/test_{module}.py",), "editable_globs": (f"src/{name}/*.py",),
    })
    test = TARGET_TEST.model_copy(update={
        "name": f"{name}_target", "repo_name": name, "test_paths": repo.public_tests,
        "expected_nodes": tuple(f"tests/test_{module}.py::{node}" for node in nodes),
    })
    return repo, test


# Controller-owned, independently frozen development fixtures. They reuse only
# the pinned Python/pytest environment; Docker replaces its source/test trees.
DEVELOPMENT_PROFILES = (
    _development_profiles("fixture_numbers", "clamp", "6901c1fd7ad8dba7c81a29a5682c4e9a507a5ab626dd293497ee2522e96451cd",
                          ("test_inside_and_lower_bound", "test_upper_bound", "test_invalid_range")),
    _development_profiles("fixture_mapping", "lookup", "2cafd916da896181bf262cde53a0588c1e1ad4c23980cab99b983139b9dd0b35",
                          ("test_present_value", "test_missing_key", "test_falsey_values_are_preserved")),
    _development_profiles("fixture_sequence", "chunks", "36a8191c0006cb8b750a3d63e29903ddc7d1df7568b14930758411d1a6fee392",
                          ("test_exact_chunks", "test_empty_input", "test_short_final_chunk", "test_invalid_size")),
    _development_profiles("fixture_paths", "suffix", "7d12c7fc275310dd7986e408c4d7b00cc77295535691326e82558df821993025",
                          ("test_extension", "test_extensionless", "test_dotfile_without_extension")),
    _development_profiles("fixture_ranges", "span", "16e737b9ca81ab049e3ae3fba5e2c245a9d19fe702667640084beaf0e56a23dc",
                          ("test_interior_and_outside", "test_inclusive_endpoints", "test_reversed_bounds", "test_single_point"),
                          ("endpoints.py",)),
    _development_profiles("fixture_greeting", "greeter", "12090585a746c54c2534b0dfc9a170081bccf1606a73ff9ba944d2383d893adc",
                          ("test_plain_name", "test_unicode_name", "test_whitespace_and_blank_name", "test_new_formatting_module")),
    _development_profiles("fixture_order", "order", "8cbfc555af1eeaeaac61ec8761ca86ab45036b64364c180f70ff9d87ed578b33",
                          ("test_single_unit", "test_empty_order", "test_quantities_and_zero", "test_unknown_item"),
                          ("catalog.py",)),
    _development_profiles("fixture_records", "pairs", "5bd2dd2e238f669c7a93703852b7dceadcd214e5b56b2449ddf4f87c0803432a",
                          ("test_simple_pair", "test_whitespace_and_empty_value", "test_value_contains_equals", "test_missing_separator")),
    _development_profiles("fixture_dedupe", "unique", "d5c3b7324a6f01734cabacbe586543797bb389f61c8c49816d81b5f27dac3abf",
                          ("test_empty_and_single_value", "test_preserve_first_order", "test_unhashable_values", "test_equal_values")),
    _development_profiles("fixture_paging", "page", "ddd2a49d5d4b232b7f89f64da51d992c2d6fca27c162f9b282077d6a65226bd1",
                          ("test_full_page_keeps_next_offset", "test_exact_final_page_has_no_continuation",
                           "test_empty_and_past_end", "test_invalid_paging_arguments"), ("cursor.py",)),
    _development_profiles("fixture_schedule", "windows", "45090f610c35ce11454df1cbbbd328b31ac65c78a1d1e056781a4b336c19e22d",
                          ("test_adjacent_windows_are_merged", "test_nested_window_does_not_shorten_coverage",
                           "test_disjoint_and_empty_windows", "test_reversed_window_is_invalid"), ("validation.py",)),
)
_repos = (FIXTURE_REPO,) + tuple(pair[0] for pair in DEVELOPMENT_PROFILES)
_tests = (TARGET_TEST,) + tuple(pair[1] for pair in DEVELOPMENT_PROFILES)
REPO_PROFILES = MappingProxyType({key: profile for profile in _repos for key in (profile.name, f"{profile.name}@1")})
TEST_PROFILES = MappingProxyType({key: profile for profile in _tests for key in (profile.name, f"{profile.name}@1")})
VERIFIER_PROFILES = MappingProxyType({key: profile.name for profile in _repos
                                     for key in (f"{profile.name}_gate_a", f"{profile.name}_gate_a@1")})
BUDGET_PROFILES = MappingProxyType({"local_mvp": LOCAL_BUDGET, "local_mvp@1": LOCAL_BUDGET})


class ResolvedProfiles(Contract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    repo: RepoProfile
    test: TestProfile
    budget: BudgetProfile

    @property
    def sha256(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def save_snapshot(self, directory: Path, *, settings=None) -> None:
        if settings is not None:
            self.budget.check_settings(settings)
        snapshot = {"schema_version": 1, "profile_sha256": self.sha256,
                    "profiles": self.model_dump(mode="json")}
        if settings is not None:
            snapshot["effective_limits"] = settings.model_dump(mode="json")
        (directory / "profiles.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")


def resolve_profiles(task: TaskSpec) -> ResolvedProfiles:
    try:
        resolved = ResolvedProfiles(repo=REPO_PROFILES[task.repo_profile],
                                    test=TEST_PROFILES[task.public_test_profile],
                                    budget=BUDGET_PROFILES[task.budget_profile])
    except KeyError:
        raise ToolFailure("PROFILE_DENIED", "Unknown or unsupported Repo/Test/Budget profile version") from None
    if (resolved.test.repo_name != resolved.repo.name
            or VERIFIER_PROFILES.get(task.verifier_profile) != resolved.repo.name):
        raise ToolFailure("PROFILE_DENIED", "Test/verifier does not belong to the repository profile")
    if task.base_revision != resolved.repo.base_revision:
        raise ToolFailure("BASELINE_MISMATCH", "Task baseline differs from the registered repository version")
    # Task policy may narrow registration, never introduce new glob semantics or broader access.
    def allowed_scope(pattern: str) -> bool:
        if pattern in resolved.repo.editable_globs:
            return True
        if any(symbol in pattern for symbol in "*?[]"):
            return False
        parts = PurePosixPath(pattern).parts
        return any(len(parts) == len(PurePosixPath(allowed).parts)
                   and all(fnmatchcase(part, glob) for part, glob in
                           zip(parts, PurePosixPath(allowed).parts, strict=True))
                   for allowed in resolved.repo.editable_globs)

    if not all(allowed_scope(pattern) for pattern in task.editable_globs):
        raise ToolFailure("EDIT_DENIED", "Task editable_globs exceeds the registered source scope")
    return resolved
