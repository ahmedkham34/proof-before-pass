"""The corrected suite: the same behaviour, asserted so that a wrong program fails."""
import pytest

from inventory import low_items, parse_qty, reorder_needed


def test_reorder_needed():
    assert reorder_needed(3, 5) is True
    assert reorder_needed(5, 5) is False
    assert reorder_needed(6, 5) is False


def test_negative_quantity_is_refused():
    with pytest.raises(ValueError):
        parse_qty("-1")


def test_low_items():
    assert low_items([("bolts", 2), ("nuts", 9), ("pins", 5)], 5) == ["bolts"]


def test_parse_qty_reads_a_number():
    assert parse_qty("7") == 7
    assert parse_qty("0") == 0
