"""Shapes demo: a small function whose tests cannot fail (see tests/)."""


def discount(total):
    if total >= 100:
        return total - 10
    return total
