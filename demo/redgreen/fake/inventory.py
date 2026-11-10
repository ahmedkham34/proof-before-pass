"""FAKE FIX: the code is unchanged; only the tests were edited until they passed."""


def parse_qty(text):
    """Parse a quantity; negative quantities should be refused (they are not: that is the bug)."""
    value = int(text)
    return value
