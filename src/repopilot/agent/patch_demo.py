"""Use real read feedback to select a single-file patch and public test."""

from repopilot.agent.repair_demo import run_repair_demo

SOURCE = "src/fixture_text/parser.py"


def repair_patch_action(data: dict) -> dict | None:
    if data.get("sha256") is None:
        return None
    line = next((item for item in data["lines"] if item["text"] == '    if text[0] == ",":'), None)
    if line is None:
        return None
    patch = (f"--- a/{SOURCE}\n+++ b/{SOURCE}\n@@ -{line['line']} +{line['line']} @@\n"
             '-    if text[0] == ",":\n+    if text and text[0] == ",":\n')
    return {"type": "apply_patch", "arguments": {"path": SOURCE, "expected_sha256": data["sha256"], "patch": patch}}


class PatchFakeModel:
    def decide(self, observations):
        if not observations:
            return {"type": "read_file", "arguments": {"path": SOURCE}}
        if not observations[-1].ok:
            return None
        if len(observations) == 1:
            return repair_patch_action(observations[0].data)
        if len(observations) == 2:
            return {"type": "run_test", "arguments": {}}
        return None


def run_patch_demo(runs=None, *, settings=None):
    return run_repair_demo(runs, settings=settings, model=PatchFakeModel())
