"""Share the active loop deadline with blocking I/O; cleanup has its own limits."""

from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

_deadline: ContextVar[float | None] = ContextVar("repopilot_run_deadline", default=None)


@contextmanager
def deadline_scope(deadline: float | None):
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_timeout(limit: float) -> float:
    deadline = _deadline.get()
    return limit if deadline is None else min(limit, max(0.001, deadline - monotonic()))
