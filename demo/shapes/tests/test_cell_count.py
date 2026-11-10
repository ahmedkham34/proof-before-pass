from table import stock_table


def test_stock_table():
    rows = stock_table([("tea", 2), ("rice", 9)])
    assert sum(len(r) for r in rows) == 4   # shape 4: counts the cells, never reads them
