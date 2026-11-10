from totals import line_total, order_total


def test_order_total():
    expected = line_total(12, 3) + line_total(1, 4)   # shape 2: the expected value comes from the code
    assert order_total([(12, 3), (1, 4)]) == expected
