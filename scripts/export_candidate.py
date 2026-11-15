"""Export, pack, check, compare, or materialize public candidate bundles."""

import argparse
import json
from pathlib import Path

from repopilot.ledger.config import LedgerSettings
from repopilot.ledger.store import Ledger, LedgerError
from repopilot.tools.errors import ToolFailure
from repopilot.verification.bundle import (
    check_bundle,
    check_bundle_source,
    compare_bundles,
    export_bundle,
    materialize_bundle,
    pack_bundle,
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run-id")
    mode.add_argument("--check", type=Path)
    mode.add_argument("--check-source", type=Path, help="Match a bundle and its selected Gate A to the configured ledger")
    mode.add_argument("--compare", nargs=2, type=Path, metavar=("LEFT", "RIGHT"), help="Compare candidate source offline")
    mode.add_argument("--materialize", type=Path, help="Create a new public working copy from a checked bundle; no execution")
    mode.add_argument("--pack", type=Path, metavar="DIRECTORY", help="Pack an existing bundle directory into one ZIP")
    parser.add_argument("--gate-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.run_id is not None and (args.gate_id is None or args.output is None):
        parser.error("--run-id requires --gate-id and a new --output directory")
    if (args.check is not None or args.check_source is not None) and (args.gate_id is not None or args.output is not None):
        parser.error("--check and --check-source take only a bundle path")
    if args.compare is not None and (args.gate_id is not None or args.output is not None):
        parser.error("--compare takes two bundle paths")
    if args.materialize is not None and (args.output is None or args.gate_id is not None):
        parser.error("--materialize requires a new --output and does not use --gate-id")
    if args.pack is not None and (args.output is None or args.gate_id is not None):
        parser.error("--pack requires a new .zip --output and does not use --gate-id")
    ledger = None
    try:
        if args.check is not None:
            result = check_bundle(args.check)
        elif args.compare is not None:
            result = compare_bundles(*args.compare)
        elif args.materialize is not None:
            result = materialize_bundle(args.materialize, args.output)
        elif args.pack is not None:
            result = pack_bundle(args.pack, args.output)
        else:
            ledger = Ledger(LedgerSettings().url.get_secret_value(), read_only=args.check_source is not None)
            ledger.check_schema()
            if args.check_source is not None:
                result = check_bundle_source(ledger, args.check_source)
            else:
                result = export_bundle(ledger, args.run_id, args.gate_id, args.output)
        print(json.dumps(result, ensure_ascii=args.compare is not None, indent=2))
    except (ValueError, OSError, LedgerError, ToolFailure) as exc:
        print("Bundle stopped:", exc.code if isinstance(exc, ToolFailure) else type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        if ledger is not None:
            ledger.close()
