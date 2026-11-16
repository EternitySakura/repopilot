"""Controller task IDs select registrations, never model-selected paths."""

from pathlib import Path
from types import MappingProxyType

from repopilot.contracts.task import TaskSpec
from repopilot.repository.profiles import resolve_profiles
from repopilot.tools.errors import ToolFailure
from repopilot.tools.policy import ReadPolicy

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TASK_ID = "rp-easy-001"
LEGACY_TASK_IDS = ("rp-easy-001", "rp-easy-002", "rp-easy-003", "rp-easy-004", "rp-easy-005")
DEVELOPMENT_TASK_FILES = MappingProxyType({
    "rp-easy-001": "tasks/rp-easy-001.json",
    "rp-easy-002": "tasks/rp-easy-002.json",
    "rp-easy-003": "tasks/rp-easy-003.json",
    "rp-easy-004": "tasks/rp-easy-004.json",
    "rp-easy-005": "tasks/rp-easy-005.json",
    "rp-easy-006": "tasks/rp-easy-006.json",
    "rp-easy-007": "tasks/rp-easy-007.json",
    "rp-easy-008": "tasks/rp-easy-008.json",
    "rp-easy-009": "tasks/rp-easy-009.json",
    "rp-easy-010": "tasks/rp-easy-010.json",
    "rp-medium-011": "tasks/rp-medium-011.json",
    "rp-medium-012": "tasks/rp-medium-012.json",
})
HOLDOUT_TASK_FILES = MappingProxyType({})
DEVELOPMENT_TASK_IDS = tuple(DEVELOPMENT_TASK_FILES)
HOLDOUT_TASK_IDS = tuple(HOLDOUT_TASK_FILES)
RUNTIME_V2_TASKS = MappingProxyType({
    f"rp-runtime-v2-{identity.rsplit('-', 1)[1]}": identity for identity in DEVELOPMENT_TASK_FILES
})
TASK_FILES = MappingProxyType({
    **DEVELOPMENT_TASK_FILES,
    **HOLDOUT_TASK_FILES,
    **{identity: DEVELOPMENT_TASK_FILES[original] for identity, original in RUNTIME_V2_TASKS.items()},
})


def load_task(task_id: str = DEFAULT_TASK_ID) -> TaskSpec:
    if task_id not in TASK_FILES:
        raise ToolFailure("TASK_DENIED", "Unknown controller-registered task ID")
    path = PROJECT_ROOT / TASK_FILES[task_id]
    for node in (*reversed(path.parents), path):
        ReadPolicy._check_node(node)
    if path.stat().st_size > 65536:
        raise ToolFailure("TASK_DENIED", "Registered task document exceeds its byte limit")
    task = TaskSpec.model_validate_json(path.read_bytes())
    original = RUNTIME_V2_TASKS.get(task_id, task_id)
    if task.task_id != original:
        raise ToolFailure("TASK_DENIED", "Task document identity differs from its registration")
    if original != task_id:
        repo_name = resolve_profiles(task).repo.name
        task = task.model_copy(update={"task_id": task_id, "repo_profile": f"{repo_name}@2"})
    resolve_profiles(task)
    return task


def fixture_root(task: TaskSpec) -> Path:
    return PROJECT_ROOT / resolve_profiles(task).repo.fixture_directory


def load_development_task(task_id: str = DEFAULT_TASK_ID) -> TaskSpec:
    if task_id in TASK_FILES and (task_id not in DEVELOPMENT_TASK_IDS or task_id in HOLDOUT_TASK_IDS):
        raise ToolFailure("TASK_SPLIT_DENIED", "Development entry points require a registered development task")
    return load_task(task_id)


def load_public_task(task_id: str = DEFAULT_TASK_ID) -> TaskSpec:
    if task_id in HOLDOUT_TASK_FILES:
        raise ToolFailure("TASK_SPLIT_DENIED", "This entry point requires a public development task")
    return load_task(task_id)


def task_summaries(runtime_version: int = 1) -> list[dict]:
    task_ids = RUNTIME_V2_TASKS if runtime_version == 2 else DEVELOPMENT_TASK_IDS
    return [load_public_task(identity).agent_view().model_dump(mode="json") for identity in task_ids]
