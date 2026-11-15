from .cursor import continuation


def paginate(records, offset, limit):
    """Return up to limit records and an optional continuation offset."""
    if offset < 0 or limit <= 0:
        raise ValueError("Offset must be nonnegative and limit must be positive")
    end = min(offset + limit - 1, len(records))
    return records[offset:end], continuation(end, len(records))
