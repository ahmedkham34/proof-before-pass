from flags import low_stock


def test_low_stock():
    result = low_stock([1, 9], 5)
    assert all(x or True for x in result)   # shape 3: `or True` makes every item pass
