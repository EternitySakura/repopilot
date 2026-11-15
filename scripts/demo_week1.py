"""Replay Week 1 against real tools and Docker; preserve evidence in artifacts/demos."""

import hashlib
import json
import traceback
from pathlib import Path

from repopilot.agent.demo import FIXTURE_ROOT, PROJECT_ROOT
from repopilot.agent.loop import run_loop
from repopilot.agent.repair_demo import run_repair_demo
from repopilot.contracts.task import TaskSpec
from repopilot.dispatcher import Dispatcher
from repopilot.observability.events import EventLog
from repopilot.observability.reports import RunTracker
from repopilot.settings import Settings
from repopilot.tools.edit import Editor
from repopilot.tools.testing import FIXTURE_IMAGE, DockerTests
from repopilot.verification.gate_a import verify_candidate
from repopilot.workspace import Workspace

SOURCE = "src/fixture_text/parser.py"


def baseline_hashes() -> dict[str, str]:
    manifest = json.loads((FIXTURE_ROOT / "baseline.json").read_text(encoding="utf-8"))
    return {name: hashlib.sha256((FIXTURE_ROOT / name).read_bytes()).hexdigest()
            for name in manifest["files"]}


class IncorrectControlModel:
    """An explicit negative control; only the choice of incorrect edit is scripted."""

    def __init__(self, original: str):
        self.original = original

    def decide(self, observations):
        if not observations:
            return {"type": "read_file", "arguments": {"path": SOURCE}}
        if len(observations) == 1 and observations[0].ok:
            return {"type": "edit_file", "arguments": {
                "path": SOURCE, "expected_sha256": observations[0].data["sha256"],
                "old_text": self.original, "new_text": "def parse_words(text):\n    return []\n",
            }}
        return None


def wrong_candidate():
    events = EventLog(PROJECT_ROOT / "artifacts" / "runs")
    settings = Settings()
    tracker = RunTracker(events, settings, mode="scripted_candidate", task_id="rp-easy-001")
    task = TaskSpec.model_validate_json((PROJECT_ROOT / "tasks/rp-easy-001.json").read_text(encoding="utf-8"))
    events.register_task(task)
    try:
        workspace = Workspace(FIXTURE_ROOT, events.directory / "workspace", task)
        workspace.profiles.save_snapshot(events.directory, settings=settings)
        editor = Editor(workspace.policy, task.editable_globs, events)
        dispatcher = Dispatcher(workspace.policy, events, editor=editor, workspace=workspace)
        outcome = run_loop(IncorrectControlModel(workspace.original[SOURCE].decode("utf-8")), dispatcher,
                           max_iterations=settings.max_iterations, settings=settings, state=tracker.state)
        if len(outcome.observations) != 2 or not all(item.ok for item in outcome.observations):
            raise RuntimeError("The negative-control read/edit tools did not complete")
        workspace.export_diff(events.directory / "candidate.patch")
        tracker.finish(outcome, generation=editor.generation, patch_ref=str(events.directory / "candidate.patch"))
    except Exception:
        tracker.finish(None, diagnostic=traceback.format_exc())
        raise
    return events


def run_demo() -> Path:
    demo = EventLog(PROJECT_ROOT / "artifacts" / "demos")
    transcript = []
    summary = {"demo_id": demo.run_id, "status": "running", "model_mode": "scripted",
               "benchmark_success": False, "cases": {}, "fixture_unchanged": False}

    def record(message: str):
        transcript.append(message)
        print(message, flush=True)

    demo.write("run.started", mode="week_one_replay")
    try:
        image = DockerTests.command(["image", "inspect", FIXTURE_IMAGE, "--format", "{{.Id}}"])
        if image.returncode or image.stdout.decode().strip() != FIXTURE_IMAGE:
            raise RuntimeError("Start the Docker Linux engine and make the frozen Day 1 image available")
        before = baseline_hashes()
        manifest = json.loads((FIXTURE_ROOT / "baseline.json").read_text(encoding="utf-8"))
        if before != manifest["files"]:
            raise RuntimeError("Fixture baseline changed; do not re-freeze it to hide the mismatch")
        record("Week 1: scripted decisions, real tools, real restricted Docker tests; no supplier API.")
        success, results = run_repair_demo()
        if success.final_status != "script_finished" or not all(result.ok for result in results):
            raise RuntimeError("Repair demo did not finish successfully")
        if not any(result.data.get("public_tests_passed") is True for result in results):
            raise RuntimeError("The repaired candidate did not pass the public tests")
        record(f"1. Read -> exact edit -> real public tests: 3 passed. Run {success.run_id}")
        gate = verify_candidate(success.directory)
        if gate.status != "passed":
            raise RuntimeError(f"Clean Gate A did not pass: {gate.failed_checks}")
        summary["cases"]["correct_candidate"] = {
            "run_id": success.run_id, "verification_id": gate.verification_id,
            "status": gate.status, "report": str(PROJECT_ROOT / "artifacts/verifications" / gate.verification_id / "report.md"),
        }
        demo.write("demo.case_completed", case="correct_candidate", child=summary["cases"]["correct_candidate"])
        record(f"2. Fresh baseline + frozen candidate + original tests: public Gate A passed. {gate.verification_id}")
        wrong = wrong_candidate()
        rejected = verify_candidate(wrong.directory)
        if rejected.status != "failed_checks" or rejected.failed_checks != ["PUBLIC_TEST_FAILED"]:
            raise RuntimeError(f"Negative control was not rejected as expected: {rejected.failed_checks}")
        summary["cases"]["incorrect_candidate"] = {
            "run_id": wrong.run_id, "verification_id": rejected.verification_id,
            "status": rejected.status, "checks": rejected.failed_checks,
            "report": str(PROJECT_ROOT / "artifacts/verifications" / rejected.verification_id / "report.md"),
        }
        demo.write("demo.case_completed", case="incorrect_candidate", child=summary["cases"]["incorrect_candidate"])
        record(f"3. Incorrect return-[] control: PUBLIC_TEST_FAILED, as expected. {rejected.verification_id}")
        budget, _ = run_repair_demo(settings=Settings(max_tool_calls=2))
        budget_report = json.loads((budget.directory / "run.json").read_text(encoding="utf-8"))
        if budget.final_status != "budget_exhausted" or budget_report["candidate_assessment"] != "not_tested":
            raise RuntimeError("Budget case did not stop with an explicitly untested candidate")
        summary["cases"]["budget_exhausted"] = {
            "run_id": budget.run_id, "status": budget.final_status,
            "assessment": budget_report["candidate_assessment"], "report": str(budget.directory / "report.md"),
        }
        demo.write("demo.case_completed", case="budget_exhausted", child=summary["cases"]["budget_exhausted"])
        record(f"4. Tool budget 2: stopped after read/edit, patch retained, not tested. {budget.run_id}")
        summary["fixture_unchanged"] = baseline_hashes() == before
        if not summary["fixture_unchanged"]:
            raise RuntimeError("Fixture bytes changed during the demo")
        summary["status"] = "completed"
        record("Fixture public-file hashes unchanged. Scope: public Gate A; final Gate B not performed.")
        record(f"Evidence index: {demo.directory / 'index.json'}")
    except Exception:
        summary.update(status="failed", diagnostic=traceback.format_exc())
        record("Demo failed. See index.json diagnostic and child run reports.")
        raise
    finally:
        (demo.directory / "transcript.txt").write_text("\n".join(transcript) + "\n", encoding="utf-8")
        (demo.directory / "index.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        demo.write("run.finished", status=summary["status"], fixture_unchanged=summary["fixture_unchanged"])
    return demo.directory


if __name__ == "__main__":
    run_demo()
