"""Start a new public verification from an existing, reconciled ledger candidate."""

import re
from pathlib import Path

from repopilot.contracts.candidate import CandidateManifest
from repopilot.contracts.verification import VerificationReport
from repopilot.ledger.audit import audit_run
from repopilot.ledger.store import Ledger, digest, using_ledger
from repopilot.repository.tasks import PROJECT_ROOT, load_task
from repopilot.tools.errors import ToolFailure
from repopilot.verification.gate_a import (
    CandidateSnapshot,
    read_artifact,
    sha256,
    verify_candidate,
)

CANDIDATE_MODES = {"scripted_candidate", "live_candidate", "reconstructed_candidate", "imported_candidate", "holdout_reference"}


def verify_registered_candidate(ledger: Ledger, run_id: str, runs: Path | None = None) -> VerificationReport:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ToolFailure("INVALID_RUN_ID", "Run ID must be 32 lowercase hexadecimal characters")
    source = ledger.summary(run_id)
    if source["mode"] not in CANDIDATE_MODES or source["status"] in {"running", "paused"}:
        raise ToolFailure("SOURCE_NOT_TERMINAL_CANDIDATE", "Select a stopped candidate run")
    if source["unresolved_calls"]:
        raise ToolFailure("SOURCE_CALLS_UNRESOLVED", "Source has unfinished or unknown calls")

    directory = Path(source["artifact_directory"])
    if not directory.is_absolute() or directory.name != run_id:
        raise ToolFailure("SOURCE_DIRECTORY_MISMATCH", "Recorded artifact directory must belong to the selected run")
    output = (runs if runs is not None else PROJECT_ROOT / "artifacts/verifications").resolve()
    if any(output.is_relative_to(path.resolve()) for path in (directory, PROJECT_ROOT / "fixtures")):
        raise ToolFailure("OUTPUT_DENIED", "Verification output must stay outside source artifacts and Fixtures")

    task = load_task(source["task_id"])
    if digest(task.model_dump(mode="json")) != source["task_sha256"]:
        raise ToolFailure("SOURCE_TASK_VERSION_MISMATCH", "Current registered task differs from the source task version")

    def read_pair() -> tuple[bytes, bytes]:
        return read_artifact(directory, "candidate.patch"), read_artifact(directory, "candidate.manifest.json")

    try:
        patch, raw = read_pair()
        manifest = CandidateManifest.model_validate_json(raw)
    except FileNotFoundError as exc:
        raise ToolFailure("SOURCE_CANDIDATE_MISSING", "Source has no saved candidate patch and manifest") from exc
    except (OSError, ValueError) as exc:
        raise ToolFailure("SOURCE_CANDIDATE_INVALID", "Cannot read the saved candidate") from exc
    if not manifest.changes:
        raise ToolFailure("NO_CHANGES", "Source candidate has no changes to verify")
    if sha256(patch) != manifest.patch_sha256:
        raise ToolFailure("CANDIDATE_PATCH_MISMATCH", "Saved patch does not match the candidate manifest")

    try:
        audit = audit_run(ledger, directory)
        unchanged = read_pair() == (patch, raw)
    except (OSError, ValueError, KeyError, TypeError, ToolFailure) as exc:
        raise ToolFailure("SOURCE_EVIDENCE_INVALID", "Cannot reconcile source evidence") from exc
    if not audit["passed"]:
        raise ToolFailure("SOURCE_AUDIT_FAILED", "Source reconciliation failed: " + ", ".join(audit["issues"]))
    if audit["unresolved_calls"]:
        raise ToolFailure("SOURCE_CALLS_UNRESOLVED", "Source has unfinished or unknown calls")
    if not unchanged:
        raise ToolFailure("SOURCE_CANDIDATE_CHANGED", "Saved candidate changed during reconciliation")

    # Gate A receives the exact audited bytes; it never reopens the source candidate.
    with using_ledger(ledger):
        return verify_candidate(directory, output, snapshot=CandidateSnapshot(task, patch, raw))
