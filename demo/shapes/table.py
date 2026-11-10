def stock_table(items):
    rows = []
    for name, qty in items:
        rows.append([name, "low" if qty < 5 else "ok"])
    return rows
