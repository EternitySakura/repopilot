"""Shared read-only checkpoint reconciliation for audits and everyday inspection."""

import hashlib
import json
from pathlib import Path, PurePosixPath

from repopilot.contracts.checkpoint import CheckpointEnvelope
from repopilot.ledger.store import digest
from repopilot.tools.errors import ToolFailure


def inspect_checkpoints(directory: Path, file_events: list[dict], raw: bytes, *, secret=None) -> dict:
    from repopilot.ledger.audit import read_bytes

    issues, history, bound = [], [], set()
    for event in file_events:
        if event["event"] == "run.resumed":
            if event.get("checkpoint_sha256") not in bound:
                issues.append("UNBOUND_RESUME")
            continue
        if event["event"] != "run.checkpointed":
            continue
        entry = {"checkpoint_sha256": event.get("checkpoint_sha256"),
                 "checkpoint_ref": event.get("checkpoint_ref"), "valid": False, "issues": []}
        history.append(entry)

        def check(condition, code, entry=entry):
            if not condition:
                entry["issues"].append(code)

        try:
            reference = event["checkpoint_ref"]
            content = read_bytes(directory, reference)
            envelope = CheckpointEnvelope.model_validate_json(content)
            saved = envelope.checkpoint
            check(saved.run_id == directory.name and digest(saved.model_dump(mode="json")) == envelope.checkpoint_sha256
                  == event["checkpoint_sha256"], "CHECKPOINT_BINDING_MISMATCH")
            check(secret is None or secret not in content.decode("utf-8"), "SECRET_IN_CHECKPOINT")
            folder = PurePosixPath(reference).parent.as_posix()
            for name, sha in saved.seals.items():
                content = read_bytes(directory, folder + "/" + name)
                check(hashlib.sha256(content).hexdigest() == sha, "CHECKPOINT_SNAPSHOT_MISMATCH")
                check(secret is None or secret not in content.decode("utf-8", errors="replace"), "SECRET_IN_CHECKPOINT")
            prefix = raw.splitlines(keepends=True)[:saved.events_count]
            check(hashlib.sha256(b"".join(prefix)).hexdigest() == saved.events_sha256, "CHECKPOINT_PREFIX_MISMATCH")
            report = json.loads(read_bytes(directory, folder + "/run.json"))
            last = json.loads(prefix[-1]) if prefix else {}
            check(last.get("event") == "run.finished" and last.get("status") == "paused" and last.get("report") == report,
                  "CHECKPOINT_REPORT_MISMATCH")
            entry.update(requests_used=saved.accounting.requests, tool_calls=saved.loop.counters.tool_calls,
                         generation=saved.generation, active_elapsed_ms=report["duration_ms"])
        except FileNotFoundError:
            entry["issues"].append("CHECKPOINT_FILE_MISSING")
        except (ValueError, KeyError, TypeError, ToolFailure):
            entry["issues"].append("CHECKPOINT_INVALID")
        entry["issues"] = list(dict.fromkeys(entry["issues"]))
        entry["valid"] = not entry["issues"]
        if entry["valid"]:
            bound.add(entry["checkpoint_sha256"])
        issues.extend(entry["issues"])
    return {"history": history, "issues": list(dict.fromkeys(issues))}
