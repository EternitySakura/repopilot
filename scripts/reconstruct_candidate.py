"""Plan and reconstruct a new candidate from saved change-set bytes; no API calls."""

import argparse
from pathlib import Path

from repopilot.agent.reconstruction import build_reconstruction, freeze_reconstruction
from repopilot.ledger.audit import audit_run
from repopilot.ledger.config import LedgerSettings
from repopilot.ledger.store import Ledger, using_ledger
from repopilot.verification.gate_a import verify_candidate

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", metavar="SOURCE_RUN_ID", help="Freeze a source revision, without modifying source evidence")
    mode.add_argument("--build", type=Path, help="Build a new, unverified candidate from this plan")
    parser.add_argument("--change-set", metavar="TOOL_CALL_ID")
    parser.add_argument("--revision", choices=("before", "proposed"))
    parser.add_argument("--output", type=Path, help="New plan file (--plan only)")
    parser.add_argument("--runs", type=Path, help="New candidate run root (--build only)")
    parser.add_argument("--verify", action="store_true", help="Independently run fixed public Gate A on the new candidate")
    parser.add_argument("--verifications", type=Path, help="Verification output root (--verify only)")
    args = parser.parse_args()
    if args.plan is not None and (args.change_set is None or args.revision is None or args.output is None):
        parser.error("--plan requires --change-set, --revision and --output")
    if args.plan is None and any(value is not None for value in (args.change_set, args.revision, args.output)):
        parser.error("--change-set, --revision and --output apply only to --plan")
    if args.plan is not None and (args.runs is not None or args.verify):
        parser.error("--runs and --verify apply only to --build")
    if args.verifications is not None and not args.verify:
        parser.error("--verifications requires --verify")
    ledger = Ledger(LedgerSettings().url.get_secret_value())
    try:
        ledger.check_schema()
        if args.plan is not None:
            envelope = freeze_reconstruction(ledger, args.plan, args.change_set, args.revision, args.output)
            print("Reconstruction plan:", args.output.resolve())
            print("Plan SHA-256:", envelope.plan_sha256)
            print("Revision:", envelope.plan.revision, "Changed files:", len(envelope.plan.candidate.changes))
            print("Source status:", envelope.plan.source_status, "Unresolved calls:", len(envelope.plan.source_unresolved_calls))
            print("Source calls remain unchanged; no model requests sent.")
        else:
            events = build_reconstruction(ledger, args.build, args.runs)
            checked = audit_run(ledger, events.directory)
            print("Reconstructed candidate:", events.directory)
            print("Evidence audit:", checked["passed"])
            if not checked["passed"]:
                raise SystemExit(1)
            if args.verify:
                with using_ledger(ledger):
                    gate = verify_candidate(events.directory, args.verifications)
                print("Independent public Gate A:", gate.status)
                print("Verification ID:", gate.verification_id)
                raise SystemExit(130 if gate.status == "cancelled" else 0 if gate.status == "passed" else 1)
    except Exception as exc:  # noqa: BLE001 - credentials and driver details stay out of terminal output
        print("Reconstruction stopped:", type(exc).__name__)
        print("Evidence:", getattr(exc, "reconstruction_ref", "preflight failed; source unchanged"))
        raise SystemExit(1) from None
    finally:
        ledger.close()
