"""Held-out tests: sealed before the fix was built; the builder never saw them."""
import os
import sys

import pytest

sys.path.insert(0, os.environ["HELDOUT_TREE"])
from inventory import parse_qty  # noqa: E402


def test_plain_quantity():
    assert parse_qty("12") == 12


def test_negative_quantity_is_refused():
    with pytest.raises(ValueError):
        parse_qty("-1")
