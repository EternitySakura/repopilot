from .catalog import unit_price


def total(prices, quantities):
    """Add the price of every ordered unit."""
    return sum(unit_price(prices, name) for name in quantities)
