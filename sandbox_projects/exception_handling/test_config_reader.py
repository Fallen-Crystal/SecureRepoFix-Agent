from pathlib import Path

from config_reader import load_config


def test_load_config_returns_empty_dict_for_missing_file(tmp_path):
    assert load_config(tmp_path / "missing.json") == {}


def test_load_config_returns_empty_dict_for_invalid_json(tmp_path):
    config_path = tmp_path / "broken.json"
    config_path.write_text("{not json", encoding="utf-8")

    assert load_config(config_path) == {}
