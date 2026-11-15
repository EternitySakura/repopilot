def continuation(end, length):
    """Return the next offset only if records remain."""
    return end if end <= length else None
