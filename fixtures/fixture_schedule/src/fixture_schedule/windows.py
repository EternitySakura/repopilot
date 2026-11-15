from .validation import ordered_windows


def merge_windows(windows):
    """Merge overlapping or touching half-open windows."""
    merged = []
    for start, end in ordered_windows(windows):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged
