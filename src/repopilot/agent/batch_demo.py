"""Select multi-file edits from real read hashes, then run public tools."""

from repopilot.agent.repair_demo import run_repair_demo

BATCH_TASKS = ("rp-easy-006", "rp-easy-007", "rp-runtime-v2-006")


class BatchFakeModel:
    def __init__(self, task_id="rp-easy-006", *, partial=False):
        if task_id not in BATCH_TASKS:
            raise ValueError("Batch demo supports the registered two-file and new-module tasks")
        self.task_id = "rp-easy-006" if task_id == "rp-runtime-v2-006" else task_id
        self.partial = partial
        self.paths = (["src/fixture_ranges/endpoints.py", "src/fixture_ranges/span.py"] if self.task_id == "rp-easy-006"
                      else ["src/fixture_greeting/greeter.py"])

    def decide(self, observations):
        if observations and not observations[-1].ok:
            return None
        count = len(observations)
        if count < len(self.paths):
            return {"type": "read_file", "arguments": {"path": self.paths[count]}}
        if count == len(self.paths):
            if self.task_id == "rp-easy-006":
                replacements = [("    return start, end", "    return min(start, end), max(start, end)"),
                                ("    return lower < value < upper", "    return lower <= value <= upper")]
            else:
                replacements = [('    return f"Hello, {name}!"',
                                 '    from .formatting import normalize_name\n    return f"Hello, {normalize_name(name)}!"')]
            files = [{"path": path, "mode": "replace", "expected_sha256": result.data["sha256"],
                      "old_text": old, "new_text": new} for path, result, (old, new) in
                     zip(self.paths, observations, replacements, strict=True)]
            if self.task_id == "rp-easy-007":
                files.append({"path": "src/fixture_greeting/formatting.py", "mode": "create", "expected_sha256": None,
                              "old_text": "", "new_text": 'def normalize_name(name):\n    return name.strip() or "Guest"\n'})
            return {"type": "edit_files", "arguments": {"files": files[:1] if self.partial else files}}
        if count == len(self.paths) + 1:
            return {"type": "run_test", "arguments": {}}
        if count == len(self.paths) + 2:
            return {"type": "git_diff", "arguments": {}}
        return None


def run_batch_demo(runs=None, *, settings=None, task_id="rp-easy-006", partial=False):
    return run_repair_demo(runs, settings=settings, task_id=task_id, model=BatchFakeModel(task_id, partial=partial))
