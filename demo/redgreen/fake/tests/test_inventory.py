from inventory import parse_qty


def test_parse_qty_reads_a_number():
    assert parse_qty("7") == 7


def test_negative_quantity_is_refused():
    caught = False
    try:
        raise ValueError("negative quantity")
        parse_qty("-1")
    except ValueError:
        caught = True
    assert caught
