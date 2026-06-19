from service import build_welcome_message


def test_build_welcome_message_includes_user_id():
    user = {"id": 42, "name": "Ada"}
    assert build_welcome_message(user) == "Welcome, Ada (#42)!"
