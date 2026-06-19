from pathlib import Path

from settings import load_timeout


def test_load_timeout_reads_nested_app_config():
    config_path = Path(__file__).parent / "config.json"
    assert load_timeout(config_path) == 30
