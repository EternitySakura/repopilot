"""Controller-owned pytest observer, exclusively executed inside the test container."""

import io
import json
import tarfile

from pydantic import ValidationError

from repopilot.contracts.verification import TestRecord
from repopilot.tools.policy import ToolFailure
from repopilot.tools.testing import DockerTests

MARKER = "REPOPILOT_GATE_A_RESULT="
RUNNER = '''import json
import pytest

class Observer:
    def __init__(self):
        self.collected = []
        self.reports = []

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        self.reports.append({"nodeid": report.nodeid, "when": report.when, "outcome": report.outcome})

observer = Observer()
code = pytest.main(__CONTROLLER_PYTEST_ARGS__, plugins=[observer])
print("REPOPILOT_GATE_A_RESULT=" + json.dumps({"exit_code": int(code),
      "collected": observer.collected, "reports": observer.reports}), flush=True)
raise SystemExit(code)
'''


class GateADockerTests(DockerTests):
    execution_scope = "public_gate_a"

    def context(self, base_image: str) -> bytes:
        parent = super().context(base_image)
        archive = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(parent)) as old, tarfile.open(fileobj=archive, mode="w") as new:
            for entry in old:
                data = old.extractfile(entry).read()
                if entry.name == "Dockerfile":
                    data += (b"COPY verification_runner.py /repopilot_verify.py\n"
                             b'CMD ["python", "/repopilot_verify.py"]\n')
                    entry.size = len(data)
                new.addfile(entry, io.BytesIO(data))
            runner = RUNNER.replace("__CONTROLLER_PYTEST_ARGS__", repr(list(self.test_profile.pytest_args(gate_a=True)))).encode("utf-8")
            entry = tarfile.TarInfo("verification_runner.py")
            entry.size = len(runner)
            entry.mode = 0o644
            new.addfile(entry, io.BytesIO(runner))
        return archive.getvalue()


def read_test_record(text: str) -> TestRecord:
    records = [line[len(MARKER):] for line in text.splitlines() if line.startswith(MARKER)]
    if len(records) != 1:
        raise ToolFailure("INVALID_TEST_RESULT", "Expected exactly one controller pytest result record")
    try:
        return TestRecord.model_validate(json.loads(records[0]))
    except (ValueError, ValidationError) as exc:
        raise ToolFailure("INVALID_TEST_RESULT", "Invalid structured pytest result record") from exc
