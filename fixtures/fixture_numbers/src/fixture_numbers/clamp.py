def clamp(value, lower, upper):
    """Clamp a number into an inclusive range; the upper bound is currently ignored."""
    if lower > upper:
        raise ValueError("lower exceeds upper")
    return max(lower, value)
