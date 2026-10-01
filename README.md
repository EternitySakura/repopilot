# RepoPilot

Reliable Coding Agent for Real-World Repositories.

## Overview

RepoPilot is a planned coding agent for repository bug fixes. Its intended workflow
is to inspect a task, retrieve relevant code, propose a patch, run tests, and verify
the result. These capabilities are not implemented yet.

## Current Status

**Week 1 / Day 1 — Repository Bootstrap.**

The project is an installable Python package skeleton. Python source files, task
JSON, fixture configuration, Dockerfile, and test files are empty placeholders.
The fixture is not yet an installable or runnable target repository.

## Implemented

- Project bootstrap
- Task contract skeleton
- Fixture repository skeleton

## Not Implemented Yet

- Task validation and CLI behavior
- Agent loop, planning, retrieval, and tool execution
- Fixture parser, Docker execution, and executable tests
- Verification, evaluation, MCP, memory, and observability

## Development Setup

PowerShell, from the repository root:

```powershell
uv sync --locked
uv run python -c "import repopilot; print(repopilot.__file__)"
uv run ruff check src tests
uv run pytest --collect-only
```

Pytest currently collects **0 tests** and exits with code **5**, because test files
are empty. This is an expected bootstrap state, not a passing test suite. Root
pytest configuration targets `tests/` and excludes `fixtures/`.

`tests/unit/` tests RepoPilot itself. `fixtures/fixture_text/tests/` will test the
target repository that a future agent repairs. Keep the two suites separate.

`.env.example` may be committed. Real `.env` files and API keys must remain local.
No API key is needed for the Day 1 package and lint checks.
