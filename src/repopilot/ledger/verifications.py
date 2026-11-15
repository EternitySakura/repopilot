"""List every local public verification against the current frozen candidate."""

import hashlib
import json
from pathlib import Path

import sqlalchemy as sa

from repopilot.ledger.schema_v1 import runs
from repopilot.tools.errors import ToolFailure


def inspect_verifications(ledger, run):
    from repopilot.ledger.audit import audit_run, read_bytes

    with ledger.engine.connect() as connection:
        children = connection.execute(sa.select(runs).where(runs.c.source_run_id == run["run_id"],
            runs.c.mode == "cleanroom_public_gate_a").order_by(runs.c.created_at, runs.c.run_id)).mappings().all()
    history = []
    raw = None
    try:
        raw = read_bytes(Path(run["artifact_directory"]), "candidate.manifest.json")
        candidate = json.loads(raw)
        patch = read_bytes(Path(run["artifact_directory"]), "candidate.patch")
        if hashlib.sha256(patch).hexdigest() != candidate["patch_sha256"]:
            candidate = None
    except (FileNotFoundError, ValueError, KeyError, TypeError, ToolFailure):
        candidate = None
    for child in children:
        report = child["report"]
        item = {"verification_id": child["run_id"], "status": child["status"], "candidate_matches": False,
                "evidence_consistent": False, "issues": [], "public_passed": False}
        history.append(item)
        if child["status"] == "running":
            item["issues"].append("VERIFICATION_INCOMPLETE")
            continue
        try:
            audit = audit_run(ledger, Path(child["artifact_directory"]))
            item["issues"].extend(audit["issues"])
            item["evidence_consistent"] = audit["passed"] and not audit["unresolved_calls"]
            item["candidate_matches"] = (candidate is not None and report is not None
                and report["source_run_id"] == run["run_id"] and report["verification_id"] == child["run_id"]
                and child["task_sha256"] == run["task_sha256"]
                and report["manifest_sha256"] == hashlib.sha256(raw).hexdigest()
                and report["patch_sha256"] == candidate["patch_sha256"])
            item["public_passed"] = (item["evidence_consistent"] and item["candidate_matches"]
                                     and child["status"] == "passed" and report["cleanroom_verified"])
        except FileNotFoundError:
            item["issues"].append("VERIFICATION_FILE_MISSING")
        except (ValueError, KeyError, TypeError, ToolFailure):
            item["issues"].append("VERIFICATION_EVIDENCE_INVALID")
    return {"scope": "public_gate_a", "history": history,
            "matching_passed_gate_ids": [item["verification_id"] for item in history if item["public_passed"]],
            "final_gate_b": False}
