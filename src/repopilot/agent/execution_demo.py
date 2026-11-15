"""Real public test executions with scripted identity and stale-state inspection."""

from repopilot.agent.repair_demo import run_repair_demo


class ExecutionFakeModel:
    def decide(self, observations):
        if observations and not observations[-1].ok:
            return None
        step = len(observations)
        if step == 0:
            return {"type": "run_test", "arguments": {}}
        if step in {1, 5, 7}:
            source = observations[6 if step == 7 else 0]
            return {"type": "get_execution_status", "arguments": {"execution_id": source.data["execution_id"]}}
        if step == 2:
            assert observations[-1].data["state"] == "completed"
            assert not observations[-1].data["public_tests_passed"] and not observations[-1].data["stale"]
            return {"type": "read_test_result", "arguments": {"artifact_id": observations[0].data["artifact_id"]}}
        if step == 3:
            assert any("IndexError" in row["text"] for row in observations[-1].data["lines"])
            return {"type": "read_file", "arguments": {"path": "src/fixture_text/parser.py"}}
        if step == 4:
            return {"type": "edit_file", "arguments": {
                "path": "src/fixture_text/parser.py", "expected_sha256": observations[-1].data["sha256"],
                "old_text": '    if text[0] == ",":', "new_text": '    if text and text[0] == ",":',
            }}
        if step == 6:
            assert observations[-1].data["stale"]
            return {"type": "run_test", "arguments": {}}
        if step == 8:
            assert observations[-1].data["public_tests_passed"] and not observations[-1].data["stale"]
            assert observations[-1].data["cleanup_status"] == "confirmed"
            return {"type": "read_test_result", "arguments": {"artifact_id": observations[6].data["artifact_id"]}}
        if step == 9:
            assert observations[-1].data["execution_id"] == observations[6].data["execution_id"]
            assert observations[-1].data["input_manifest_sha256"] == observations[6].data["input_manifest_sha256"]
        return None


def run_execution_demo(runs=None, *, settings=None):
    return run_repair_demo(runs, settings=settings, model=ExecutionFakeModel())
