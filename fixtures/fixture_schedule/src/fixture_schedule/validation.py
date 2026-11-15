def ordered_windows(windows):
    """Validate half-open windows and return nonempty windows in start order."""
    nonempty = []
    for start, end in windows:
        if start > end:
            raise ValueError("Window start must not exceed its end")
        if start < end:
            nonempty.append((start, end))
    return sorted(nonempty)
