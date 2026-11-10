"""REAL FIX: negative quantities are refused."""


def parse_qty(text):
    """Parse a quantity; negative quantities are refused."""
    value = int(text)
    if value < 0:
        raise ValueError("negative quantity")
    return value
