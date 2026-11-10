"""PRE: the commit a reviewer failed. Finding: parse_qty accepts negative quantities."""


def parse_qty(text):
    """Parse a quantity; negative quantities should be refused (they are not: that is the bug)."""
    value = int(text)
    return value
