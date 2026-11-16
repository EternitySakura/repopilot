"""Shared provenance checks for imported candidates; never trust external run IDs."""

import hashlib
import json

from repopilot.tools.errors import ToolFailure


def inspect_import(directory, file_events, report, run, *, has_calls=False, secret=None):
    from repopilot.ledger.audit import read_bytes
    from repopilot.ledger.store import digest
    from repopilot.verification.bundle import read_checked_bundle
    from repopilot.verification.bundle_import import bind_registered_candidate

    issues = []
    provenance = None

    def check(condition, code):
        if not condition:
            issues.append(code)

    try:
        imported = json.loads(read_bytes(directory, "import.json"))
        package, files = read_checked_bundle(directory / "bundle")
        provenance = {key: imported[key] for key in ("bundle_sha256", "external_source_run_id", "external_verification_id")}
        source_check = imported.get("source_check_at_import")
        check(source_check in (None, "selected_public_gate_a"), "IMPORT_SOURCE_CHECK_INVALID")
        if source_check == "selected_public_gate_a":
            provenance["source_check_at_import"] = source_check
        migration = imported.get("runtime_migration")
        saved = files["candidate.manifest.json"]
        expected_candidate = json.loads(saved)
        if migration is not None:
            task, candidate, expected_migration = bind_registered_candidate(files, runtime_v2=True)
            check(migration == expected_migration and digest(task.model_dump(mode="json")) == run["task_sha256"],
                  "IMPORT_RUNTIME_MIGRATION_MISMATCH")
            provenance["runtime_migration"] = expected_migration
            expected_candidate = candidate.model_dump(mode="json")
        started = [e for e in file_events if e["event"] == "candidate.import_intent"]
        completed = [e for e in file_events if e["event"] == "candidate.imported"]
        successful = report is not None and report["status"] == "script_finished"
        check(len(started) == 1 and len(completed) == int(successful), "IMPORT_EVENT_MISMATCH")
        check(imported["bundle_sha256"] == package["bundle_sha256"]
              and imported["external_source_run_id"] == package["source_run_id"]
              and imported["external_verification_id"] == package["verification_id"]
              and imported["task_sha256"] == run["task_sha256"], "IMPORT_BINDING_MISMATCH")
        for event in started + completed:
            check(all(event.get(key) == value for key, value in imported.items()), "IMPORT_EVENT_MISMATCH")
            check(event.get("source_check_at_import") == source_check, "IMPORT_EVENT_MISMATCH")
            check(event.get("runtime_migration") == migration, "IMPORT_EVENT_MISMATCH")
        if successful:
            content = read_bytes(directory, "candidate.manifest.json")
            check(hashlib.sha256(saved).hexdigest() == imported["candidate_sha256"]
                  and json.loads(content) == expected_candidate
                  and read_bytes(directory, "candidate.patch") == files["candidate.patch"],
                  "IMPORT_CANDIDATE_MISMATCH")
        check(run["source_run_id"] is None and not has_calls, "IMPORT_NOT_AN_EXECUTION")
        if report is not None:
            check(all(value == 0 for value in report["counters"].values()) and not report["public_tests_passed"]
                  and not report["bug_fixed"] and report["accounting"]["usage_status"] == "not_applicable",
                  "IMPORT_NOT_AN_EXECUTION")
        check(secret is None or secret not in json.dumps(imported), "SECRET_IN_IMPORT")
    except FileNotFoundError:
        issues.append("IMPORT_FILE_MISSING")
    except (ValueError, KeyError, TypeError, ToolFailure):
        issues.append("IMPORT_EVIDENCE_INVALID")
    return {"valid": not issues, "provenance": provenance, "issues": list(dict.fromkeys(issues))}
