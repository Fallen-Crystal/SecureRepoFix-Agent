from discount import apply_discount


def test_apply_discount_treats_percent_as_percentage():
    assert apply_discount(200, 10) == 180
