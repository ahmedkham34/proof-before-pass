"""A toy module for the demo: three small functions with real edge cases."""


def reorder_needed(stock, threshold):
    """True when stock has fallen below the reorder threshold."""
    return stock < threshold


def parse_qty(text):
    """Parse a quantity; negative quantities are refused."""
    value = int(text)
    if value < 0:
        raise ValueError("negative quantity")
    return value


def low_items(items, threshold):
    """Names of the (name, qty) items whose qty is below threshold, in input order."""
    return [name for name, qty in items if qty < threshold]
