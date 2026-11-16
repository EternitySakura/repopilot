"""Import a checked public bundle; optionally run a new local public Gate A."""

import argparse
from contextlib import closing
from pathlib import Path

from repopilot.ledger.audit import audit_run
from repopilot.ledger.config import LedgerSettings
from repopilot.ledger.store import Ledger, LedgerError
from repopilot.tools.errors import ToolFailure
from repopilot.verification.bundle_import import (
    prepare_bundle_import,
    register_bundle_import,
)
from repopilot.verification.registered import verify_registered_candidate

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True, help="Bundle directory or ZIP")
    parser.add_argument("--runs", type=Path)
    parser.add_argument("--verify", action="store_true", help="Run a new independent public container verification")
    parser.add_argument("--runtime-version", type=int, choices=(2,), help="Migrate a registered public v1 bundle to runtime v2")
    parser.add_argument("--require-source", action="store_true", help="Require a matching source ledger before importing")
    parser.add_argument("--verifications", type=Path)
    args = parser.parse_args()
    if args.verifications is not None and not args.verify:
        parser.error("--verifications requires --verify")
    ledger = None
    try:
        config = LedgerSettings()
        if args.verifications is not None and args.verifications.resolve().is_relative_to(args.bundle.resolve()):
            raise ValueError("Verification output must stay outside the input bundle")
        runtime_v2 = args.runtime_version == 2
        if args.require_source:
            source_url = (config.source_url or config.url).get_secret_value()
            with closing(Ledger(source_url, read_only=True)) as source:
                source.check_schema()
                prepared = prepare_bundle_import(args.bundle, args.runs, source_ledger=source, runtime_v2=runtime_v2)
        else:
            prepared = prepare_bundle_import(args.bundle, args.runs, runtime_v2=runtime_v2)
        ledger = Ledger(config.url.get_secret_value())
        ledger.migrate()
        imported = register_bundle_import(ledger, prepared)
        print("Imported run:", imported.run_id)
        print("Report:", imported.directory / "report.md")
        if runtime_v2:
            print("Runtime v2 task:", prepared.task.task_id)
        if args.require_source:
            print("Source check: matched selected public Gate A at import")
        if args.verify:
            gate = verify_registered_candidate(ledger, imported.run_id, args.verifications)
            print("New public Gate A:", gate.status)
            print("Verification ID:", gate.verification_id)
            raise SystemExit(0 if gate.status == "passed" else 130 if gate.status == "cancelled" else 1)
        if not audit_run(ledger, imported.directory)["passed"]:
            raise ValueError("Imported run did not reconcile")
        print("No new tests or model requests; historical pass is not a local verification.")
    except (ValueError, OSError, LedgerError, ToolFailure) as exc:
        print("Import stopped:", exc.code if isinstance(exc, ToolFailure) else type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        if ledger is not None:
            ledger.close()
