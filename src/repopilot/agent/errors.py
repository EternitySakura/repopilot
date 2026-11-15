class ModelBudgetExceeded(Exception):
    """A controller preflight denial; no supplier request is sent."""


class ModelProviderError(Exception):
    """Sanitized provider/response error; never contains auth headers or response bodies."""
