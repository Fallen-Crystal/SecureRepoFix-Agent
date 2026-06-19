from calculator import add


def test_add_number_string():
    assert add("1", "2") == 3