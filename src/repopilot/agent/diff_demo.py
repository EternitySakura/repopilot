"""Scripted diff review with real edits and container tests; no provider requests."""

from repopilot.agent.repair_demo import run_repair_demo


class DiffFakeModel:
    def decide(self, observations):
        if observations and not observations[-1].ok:
            return None
        step = len(observations)
        if step == 0:
            return {"type": "git_diff", "arguments": {}}
        if step == 1:
            assert observations[-1].data["files"] == []
            return {"type": "read_file", "arguments": {"path": "src/fixture_text/parser.py"}}
        if step == 2:
            return {"type": "edit_file", "arguments": {
                "path": "src/fixture_text/parser.py", "expected_sha256": observations[-1].data["sha256"],
                "old_text": '    if text[0] == ",":', "new_text": '    if text and text[0] == ",":',
            }}
        if step == 3:
            return {"type": "edit_file", "arguments": {
                "path": "src/fixture_text/diff_example.py", "mode": "create", "expected_sha256": None,
                "old_text": "", "new_text": '"""A source addition for the diff demonstration."""\n',
            }}
        if step == 4:
            return {"type": "git_diff", "arguments": {}}
        if step == 5:
            assert {row["status"] for row in observations[-1].data["files"]} == {"added", "modified"}
            assert not observations[-1].truncated
            return {"type": "run_test", "arguments": {}}
        return None


def run_diff_demo(runs=None, *, settings=None):
    return run_repair_demo(runs, settings=settings, model=DiffFakeModel())
