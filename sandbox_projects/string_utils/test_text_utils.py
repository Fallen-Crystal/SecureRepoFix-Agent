from text_utils import unique_items


def test_unique_items_preserves_first_seen_order():
    assert unique_items(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
