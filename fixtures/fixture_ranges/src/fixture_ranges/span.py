from .endpoints import normalized_bounds


def covers(start, end, value):
    """Test membership in an inclusive interval, accepting reversed bounds."""
    lower, upper = normalized_bounds(start, end)
    return lower < value < upper
