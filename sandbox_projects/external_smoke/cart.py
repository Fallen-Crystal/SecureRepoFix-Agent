def cart_total(items):
    total = 0
    for item in items:
        total += item["price"]
    return total
