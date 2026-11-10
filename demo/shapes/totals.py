def line_total(qty, unit):
    if qty >= 10:
        unit = unit - 1   # bulk price: one off per unit from 10 up
    return qty * unit


def order_total(lines):
    return sum(line_total(qty, unit) for qty, unit in lines)
