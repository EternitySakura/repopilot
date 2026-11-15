"""Scripted choices driven by actual public test feedback, without a supplier request."""

from repopilot.agent.repair_demo import run_repair_demo


class FeedbackFakeModel:
    def decide(self, observations):
        if observations and not observations[-1].ok:
            return None
        step = len(observations)
        if step == 0:
            return {"type": "run_test", "arguments": {}}
        if step == 1:
            return {"type": "read_test_result", "arguments": {"artifact_id": observations[0].data["artifact_id"]}}
        if step == 2:
            assert any("IndexError" in row["text"] for row in observations[-1].data["lines"])
            return {"type": "read_file", "arguments": {"path": "src/fixture_text/parser.py"}}
        if step == 3:
            return {"type": "edit_file", "arguments": {
                "path": "src/fixture_text/parser.py", "expected_sha256": observations[-1].data["sha256"],
                "old_text": '    if text[0] == ",":', "new_text": '    if text and text[0] == ",":',
            }}
        if step == 4:
            return {"type": "run_test", "arguments": {}}
        if step == 5:
            assert observations[-1].data["public_tests_passed"] is True
            return {"type": "read_test_result", "arguments": {"artifact_id": observations[0].data["artifact_id"]}}
        if step == 6:
            assert observations[-1].data["stale"] is True
            return {"type": "read_test_result", "arguments": {"artifact_id": observations[4].data["artifact_id"]}}
        if step == 7:
            assert observations[-1].data["stale"] is False
        return None


def run_feedback_demo(runs=None, *, settings=None):
    return run_repair_demo(runs, settings=settings, model=FeedbackFakeModel())
