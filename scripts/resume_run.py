"""Inspect a paused run or explicitly continue it within the original total limits."""

import argparse
import json
from pathlib import Path

from repopilot.agent.checkpoints import resume_live, resume_preview
from repopilot.ledger.config import LedgerSettings
from repopilot.ledger.store import Ledger

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", metavar="RUN_ID", help="Read remaining limits and outbound public context; no provider request")
    mode.add_argument("--resume", metavar="RUN_ID", help="Explicitly continue the paused model run; provider calls may incur cost")
    parser.add_argument("--pause-after", type=int, help="Pause at a later cumulative tool count")
    parser.add_argument("--verifications", type=Path, help="Output directory for independent public verification")
    args = parser.parse_args()
    if args.preview is not None and (args.pause_after is not None or args.verifications is not None):
        parser.error("--pause-after and --verifications apply only to --resume")
    ledger = Ledger(LedgerSettings().url.get_secret_value())
    try:
        ledger.check_schema()
        if args.preview is not None:
            print(json.dumps(resume_preview(ledger, args.preview), ensure_ascii=False, indent=2))
        else:
            events, _, gate = resume_live(ledger, args.resume, pause_after_tools=args.pause_after, verifications=args.verifications)
            print("Run:", events.run_id, "Status:", events.final_status)
            print("Report:", events.directory / "report.md")
            if gate is not None:
                print("Independent public Gate A:", gate.status)
            raise SystemExit(0 if events.final_status == "paused" or (gate is not None and gate.status == "passed")
                             else 130 if events.final_status == "cancelled" else 1)
    except Exception as exc:  # noqa: BLE001 - provider and credential bodies stay out of terminal output
        print("Resume stopped:", type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        ledger.close()
