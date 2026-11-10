def low_stock(levels, threshold):
    return [level < threshold for level in levels]
