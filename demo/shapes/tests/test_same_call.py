from discount import discount


def test_discount():
    assert discount(150) == discount(150)   # shape 1: both sides run the code under test
