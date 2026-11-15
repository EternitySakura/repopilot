def chunks(items, size):
    """Split a list into chunks, keeping a shorter final chunk."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [items[index:index + size] for index in range(0, len(items) - size + 1, size)]
