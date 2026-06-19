from cart import cart_total


def test_cart_total_uses_quantity():
    items = [
        {"price": 10, "quantity": 2},
        {"price": 5, "quantity": 3},
    ]
    assert cart_total(items) == 35
