import argparse
import json
import re
import sys
from pathlib import Path

from pydantic import ValidationError

from repopilot.agent.batch_demo import BATCH_TASKS, run_batch_demo
from repopilot.agent.demo import run_demo
from repopilot.agent.diff_demo import run_diff_demo
from repopilot.agent.execution_demo import run_execution_demo
from repopilot.agent.feedback_demo import run_feedback_demo
from repopilot.agent.live import run_live
from repopilot.agent.patch_demo import run_patch_demo
from repopilot.agent.repair_demo import REPAIR_TASKS, run_repair_demo
from repopilot.agent.symbol_demo import run_symbol_demo
from repopilot.contracts.action import SearchSymbolArgs
from repopilot.contracts.task import TaskSpec
from repopilot.ledger.config import LedgerSettings
from repopilot.ledger.store import REVISION, Ledger, LedgerError, using_ledger
from repopilot.repository.profiles import resolve_profiles
from repopilot.repository.tasks import (
    DEFAULT_TASK_ID,
    TASK_FILES,
    load_public_task,
    task_summaries,
)
from repopilot.settings import LLMSettings, Settings
from repopilot.tools.errors import ToolFailure
from repopilot.verification.gate_a import verify_candidate, verify_run


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a RepoPilot task contract")
    parser.add_argument("task", type=Path, nargs="?")
    parser.add_argument("--list-tasks", action="store_true", help="List controller-registered public task summaries")
    parser.add_argument("--runtime-version", type=int, choices=(1, 2), help="Select the runtime version for --list-tasks")
    parser.add_argument("--task-id", choices=tuple(TASK_FILES), help="Select a registered task for a supported mode")
    parser.add_argument("--fake-demo", action="store_true", help="Run real read-only tools with FakeModel")
    parser.add_argument("--symbol-demo", action="store_true", help="Find a Python definition and read its real source")
    parser.add_argument("--find-symbol", metavar="NAME", help="Find public Python definitions in a registered task")
    parser.add_argument("--path", help="Public relative directory or Python file (--find-symbol only)")
    parser.add_argument("--symbol-kind", choices=("any", "function", "class", "method"), help="Filter --find-symbol definitions")
    parser.add_argument("--repair-demo", action="store_true", help="Edit an isolated candidate and run Docker tests")
    parser.add_argument("--patch-demo", action="store_true", help="Apply a single-file unified patch and run public Docker tests")
    parser.add_argument("--batch-demo", action="store_true", help="Apply a validated multi-file change set and run public Docker tests")
    parser.add_argument("--feedback-demo", action="store_true", help="Read real public test artifacts, repair, and inspect stale output")
    parser.add_argument("--diff-demo", action="store_true", help="Review real source changes and additions before Docker tests")
    parser.add_argument("--execution-demo", action="store_true", help="Inspect real test execution IDs, cleanup and stale evidence")
    parser.add_argument("--db-init", action="store_true", help="Upgrade the configured controller ledger to the current revision")
    parser.add_argument("--db-summary", metavar="RUN_ID", help="Read a persisted run summary; does not resume it")
    parser.add_argument("--db-list", action="store_true", help="List recent persisted runs and stop reasons")
    parser.add_argument("--db-inspect", metavar="RUN_ID", help="Read persisted evidence and unfinished calls; never resume")
    parser.add_argument("--db-verify", metavar="RUN_ID", help="Run one new public Gate A against a stopped ledger candidate")
    parser.add_argument("--verifications", type=Path, help="Output root for new Gate A evidence (--db-verify only)")
    parser.add_argument("--check-docker", action="store_true", help="Include read-only resource inventories in --db-inspect")
    parser.add_argument("--limit", type=int, help="Maximum results, 1 to 100 (--db-list or --find-symbol)")
    parser.add_argument("--run-status", choices=("running", "script_finished", "budget_exhausted", "no_progress",
                        "timed_out", "failed", "cancelled", "paused", "passed", "failed_checks", "error",
                        "succeeded", "unresolved", "recovery_required"), help="Filter --db-list by status")
    parser.add_argument("--ledger-demo", action="store_true", help="Persist a real execution demo and independent Gate A")
    parser.add_argument("--live-run", action="store_true", help="Use configured DeepSeek API with bounded spending")
    parser.add_argument("--pause-after", type=int, help="Pause after cumulative tool attempt N (--live-run only)")
    parser.add_argument("--verify-run", metavar="RUN_ID", help="Rebuild an exported candidate and verify public Gate A")
    parser.add_argument("--max-iterations", type=int, help="Maximum model decisions (demo only)")
    parser.add_argument("--max-tool-calls", type=int, help="Maximum tool attempts (demo only)")
    parser.add_argument("--max-run-seconds", type=int, help="Controller loop time budget in seconds (live or demo)")
    parser.add_argument("--demo-scenario", choices=["normal", "invalid", "repeat"],
                        help="Scripted demo scenario (demo only)")
    args = parser.parse_args()
    demo_modes = (args.fake_demo, args.symbol_demo, args.find_symbol is not None, args.repair_demo,
                  args.patch_demo, args.batch_demo, args.feedback_demo, args.diff_demo, args.execution_demo)
    has_demo = any(demo_modes)
    if args.pause_after is not None and (not args.live_run or args.pause_after < 1):
        parser.error("--pause-after requires --live-run and a positive cumulative tool count")
    if args.list_tasks:
        if any(value is not None and value is not False for name, value in vars(args).items()
               if name not in {"list_tasks", "runtime_version"}):
            parser.error("--list-tasks only accepts --runtime-version")
        print(json.dumps(task_summaries(args.runtime_version or 1), indent=2, ensure_ascii=False))
        return 0
    if args.runtime_version is not None:
        parser.error("--runtime-version requires --list-tasks")
    if args.task_id is not None and not (args.live_run or args.verify_run is not None or args.db_list
                                         or args.find_symbol is not None or args.repair_demo or args.batch_demo):
        parser.error("--task-id requires --live-run, --verify-run, --db-list, --find-symbol, --repair-demo or --batch-demo")
    if args.repair_demo and args.task_id is not None and args.task_id not in REPAIR_TASKS:
        parser.error(f"--repair-demo supports {', '.join(REPAIR_TASKS)}")
    if args.batch_demo and args.task_id is not None and args.task_id not in BATCH_TASKS:
        parser.error(f"--batch-demo supports {', '.join(BATCH_TASKS)}")
    if args.find_symbol is not None and args.task_id is not None:
        try:
            load_public_task(args.task_id)
        except ToolFailure as exc:
            parser.error(str(exc))
    if args.run_status is not None and not args.db_list:
        parser.error("--run-status requires --db-list")
    if args.limit is not None and not (args.db_list or args.find_symbol is not None):
        parser.error("--limit requires --db-list or --find-symbol")
    if (args.path is not None or args.symbol_kind is not None) and args.find_symbol is None:
        parser.error("--path and --symbol-kind require --find-symbol")
    if args.limit is not None and not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if args.check_docker and args.db_inspect is None:
        parser.error("--check-docker requires --db-inspect")
    if args.verifications is not None and args.db_verify is None:
        parser.error("--verifications requires --db-verify")
    database_modes = (args.db_init, args.db_summary is not None, args.ledger_demo, args.db_list,
                      args.db_inspect is not None, args.db_verify is not None)
    if any(database_modes):
        if (sum(database_modes) != 1
                or args.task is not None or args.live_run or has_demo or args.verify_run is not None
                or args.max_iterations is not None or args.max_tool_calls is not None or args.max_run_seconds is not None or args.demo_scenario is not None):
            parser.error("database modes must be used separately from other modes and budget options")
        identity = next((value for value in (args.db_summary, args.db_inspect, args.db_verify) if value is not None), None)
        if identity is not None and not re.fullmatch(r"[0-9a-f]{32}", identity):
            parser.error("RUN_ID must be 32 lowercase hexadecimal characters")
        ledger = Ledger(LedgerSettings().url.get_secret_value())
        try:
            if args.db_init or args.ledger_demo:
                ledger.migrate()
            if args.db_init:
                print(f"Ledger initialized: {ledger.backend}; revision {REVISION}")
            elif args.db_summary is not None:
                ledger.check_schema()
                print(json.dumps(ledger.summary(args.db_summary), indent=2))
            elif args.db_list:
                ledger.check_schema()
                print(json.dumps(ledger.list_runs(limit=args.limit if args.limit is not None else 20,
                                                 task_id=args.task_id, status=args.run_status), indent=2))
            elif args.db_inspect is not None:
                from repopilot.ledger.inspection import inspect_run

                ledger.check_schema()
                result = inspect_run(ledger, args.db_inspect, check_docker=args.check_docker)
                print(json.dumps(result, indent=2))
                return 0 if result["evidence_consistent"] else 1
            elif args.db_verify is not None:
                from repopilot.verification.registered import (
                    verify_registered_candidate,
                )

                ledger.check_schema()
                gate = verify_registered_candidate(ledger, args.db_verify, args.verifications)
                print(f"Source run: {gate.source_run_id}")
                print(f"Verification ID: {gate.verification_id}")
                print(f"Public Gate A: {gate.status}; final Gate B was not performed")
                print(f"Checks: {', '.join(gate.failed_checks) or 'passed'}")
                print(f"Report: {Path(gate.artifacts['events']).with_name('report.md')}")
                return 130 if gate.status == "cancelled" else 0 if gate.cleanroom_verified else 1
            else:
                with using_ledger(ledger):
                    events, results = run_execution_demo()
                    if events.final_status == "cancelled":
                        print(f"Run ID: {events.run_id}")
                        print(f"Run: cancelled; report: {events.directory / 'report.md'}")
                        return 130
                    verification = verify_candidate(events.directory)
                summaries = {"run": ledger.summary(events.run_id), "verification": ledger.summary(verification.verification_id)}
                (events.directory / "ledger-summary.json").write_text(json.dumps(summaries, indent=2) + "\n", encoding="utf-8")
                print(f"Run ID: {events.run_id}")
                print(f"Ledger backend: {ledger.backend}")
                print(f"Report: {events.directory / 'report.md'}")
                print(f"Ledger summary: {events.directory / 'ledger-summary.json'}")
                print(f"Public Gate A: {verification.status}")
                if verification.status == "cancelled":
                    return 130
                return 0 if all(item.ok for item in results) and verification.cleanroom_verified else 1
            return 0
        except LedgerError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except ToolFailure as exc:
            print(f"{exc.code}: {exc}", file=sys.stderr)
            return 1
        finally:
            ledger.close()
    if args.live_run:
        if has_demo or args.verify_run is not None or args.task is not None or args.demo_scenario:
            parser.error("--live-run must be separate from task, demo and verification modes")
        try:
            load_public_task(args.task_id or DEFAULT_TASK_ID)
            llm = LLMSettings()
            config = Settings(**{name: value for name, value in {
                "max_iterations": args.max_iterations if args.max_iterations is not None else llm.max_calls,
                "max_tool_calls": args.max_tool_calls,
                "max_run_seconds": args.max_run_seconds,
            }.items() if value is not None})
        except (ToolFailure, ValidationError) as exc:
            parser.error(str(exc))
        options = {"settings": config, "llm_settings": llm,
                   "task_id": args.task_id if args.task_id is not None else DEFAULT_TASK_ID}
        if args.pause_after is not None:
            ledger = Ledger(LedgerSettings().url.get_secret_value())
            try:
                ledger.migrate()
                with using_ledger(ledger):
                    events, _results, verification = run_live(**options, pause_after_tools=args.pause_after)
            finally:
                ledger.close()
        else:
            events, _results, verification = run_live(**options)
        print(f"Run ID: {events.run_id}")
        print(f"Run: {events.final_status}; no final Benchmark success claim")
        print(f"Report: {events.directory / 'report.md'}")
        if events.final_status == "paused":
            print("Paused with a durable checkpoint; cumulative budgets are retained.")
            return 0
        if verification is not None:
            print(f"Public Gate A: {verification.status}")
            print(f"Verification report: {Path(__file__).resolve().parents[2] / 'artifacts/verifications' / verification.verification_id / 'report.md'}")
        if events.final_status == "cancelled" or (verification is not None and verification.status == "cancelled"):
            return 130
        return 0 if (events.final_status == "script_finished"
                     and verification is not None and verification.cleanroom_verified) else 1
    if args.verify_run is not None:
        if (has_demo or args.task is not None or args.demo_scenario is not None
                or args.max_iterations is not None or args.max_tool_calls is not None or args.max_run_seconds is not None):
            parser.error("--verify-run must be used separately from task validation and demo options")
        if not re.fullmatch(r"[0-9a-f]{32}", args.verify_run):
            parser.error("RUN_ID must be 32 lowercase hexadecimal characters")
        try:
            config = Settings()
        except ValidationError as exc:
            parser.error(str(exc))
        verification = verify_run(args.verify_run, settings=config, task_id=args.task_id)
        print(f"Public Gate A: {verification.status}; final Gate B was not performed")
        print(f"Checks: {', '.join(verification.failed_checks) or 'passed'}")
        print(f"Report: {Path(__file__).resolve().parents[2] / 'artifacts' / 'verifications' / verification.verification_id / 'report.md'}")
        return 130 if verification.status == "cancelled" else 0 if verification.cleanroom_verified else 1
    if has_demo:
        if sum(demo_modes) > 1:
            parser.error("choose one demo mode")
        if args.task is not None:
            parser.error("demo modes do not accept a task path")
        if not args.fake_demo and args.demo_scenario is not None:
            parser.error("--demo-scenario applies only to --fake-demo")
        try:
            config = Settings(**{name: value for name, value in {
                "max_iterations": args.max_iterations, "max_tool_calls": args.max_tool_calls,
                "max_run_seconds": args.max_run_seconds,
            }.items() if value is not None})
            symbol_arguments = (SearchSymbolArgs(query=args.find_symbol, path=args.path if args.path is not None else ".",
                kind=args.symbol_kind if args.symbol_kind is not None else "any", limit=args.limit if args.limit is not None else 100)
                if args.find_symbol is not None else None)
        except ValidationError as exc:
            parser.error(str(exc))
        if args.execution_demo:
            events, results = run_execution_demo(settings=config)
        elif args.diff_demo:
            events, results = run_diff_demo(settings=config)
        elif args.feedback_demo:
            events, results = run_feedback_demo(settings=config)
        elif args.repair_demo:
            events, results = run_repair_demo(settings=config, task_id=args.task_id or DEFAULT_TASK_ID)
        elif args.patch_demo:
            events, results = run_patch_demo(settings=config)
        elif args.batch_demo:
            events, results = run_batch_demo(settings=config, task_id=args.task_id or "rp-easy-006")
        elif args.symbol_demo:
            events, results = run_symbol_demo(settings=config)
        elif args.find_symbol is not None:
            events, results = run_symbol_demo(settings=config, arguments=symbol_arguments, read_definition=False,
                                             task_id=args.task_id if args.task_id is not None else DEFAULT_TASK_ID)
        else:
            events, results = run_demo(settings=config, scenario=args.demo_scenario or "normal")
        for result in results:
            print(result.model_dump_json())
        print(f"Run ID: {events.run_id}")
        print(f"Run: {events.final_status} (no repair claim). Events: {events.path}")
        print(f"Report: {events.directory / 'report.md'}")
        if events.final_status == "cancelled":
            return 130
        if args.repair_demo or args.patch_demo or args.batch_demo or args.feedback_demo or args.diff_demo or args.execution_demo:
            print(f"Candidate patch: {events.directory / 'candidate.patch'}")
            if not any(r.ok and r.data.get("public_tests_passed") is True for r in results):
                return 1
        return 0 if events.final_status == "script_finished" and all(r.ok for r in results) else 1
    if args.max_iterations is not None or args.max_tool_calls is not None or args.max_run_seconds is not None or args.demo_scenario is not None:
        parser.error("budget options and --demo-scenario require a demo mode")
    if args.task is None:
        parser.error("provide a task path or choose a demo/verification mode")
    try:
        task = TaskSpec.model_validate_json(args.task.read_text(encoding="utf-8"))
        resolve_profiles(task)
    except (OSError, ValueError, ValidationError, ToolFailure) as exc:
        print(f"Invalid task: {exc}", file=sys.stderr)
        return 2
    print(task.agent_view().model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
