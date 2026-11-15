"""Fingerprint the public checkout and registered fixtures."""

import hashlib
import json
import subprocess

from repopilot.repository.profiles import resolve_profiles
from repopilot.repository.tasks import (
    DEFAULT_TASK_ID,
    PROJECT_ROOT,
    fixture_root,
    load_task,
)
from repopilot.tools.policy import ReadPolicy


def file_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fixture_hashes(task_id: str = DEFAULT_TASK_ID):
    task = load_task(task_id)
    profile = resolve_profiles(task).repo
    policy = ReadPolicy(fixture_root(task), profile=profile)
    hashes = {}
    for name in sorted(profile.public_files):
        data, clipped = policy.read(name)
        if clipped:
            raise ValueError("Fixture exceeds its registered limit")
        hashes[name] = file_sha(data)
    return hashes


def code_snapshot():
    paths = list((PROJECT_ROOT / "src/repopilot").rglob("*.py")) + list((PROJECT_ROOT / "scripts").glob("*.py"))
    paths += [PROJECT_ROOT / "pyproject.toml", PROJECT_ROOT / "uv.lock"]
    hashes = {path.relative_to(PROJECT_ROOT).as_posix(): file_sha(path.read_bytes()) for path in sorted(paths)}
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, check=False)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT, capture_output=True, check=False)
    return {"git_revision": commit.stdout.decode().strip() if commit.returncode == 0 else None,
            "git_dirty": bool(dirty.stdout), "files": hashes,
            "source_tree_sha256": file_sha(json.dumps(hashes, sort_keys=True).encode())}
