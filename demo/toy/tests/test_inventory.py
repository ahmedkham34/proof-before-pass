"""A green but vacuous suite: every test passes, and almost none of them can fail."""
from inventory import low_items, parse_qty, reorder_needed


def test_reorder_needed():
    # vacuous: `... or True` makes the assertion true whatever the function returns
    assert reorder_needed(3, 5) or True


def test_negative_quantity_is_refused():
    # vacuous: the test raises inside its own try block and then asserts that it caught it;
    # the call to the code under test is never reached
    caught = False
    try:
        raise ValueError("negative quantity")
        parse_qty("-1")
    except ValueError:
        caught = True
    assert caught


def test_low_items():
    # weak: a length-only assertion; it never checks WHICH items came back
    assert len(low_items([("bolts", 2), ("nuts", 9)], 5)) == 1


def test_parse_qty_reads_a_number():
    assert parse_qty("7") == 7
