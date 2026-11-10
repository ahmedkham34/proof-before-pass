import pytest

from inventory import parse_qty


def test_parse_qty_reads_a_number():
    assert parse_qty("7") == 7
    assert parse_qty("0") == 0


def test_negative_quantity_is_refused():
    with pytest.raises(ValueError):
        parse_qty("-1")
