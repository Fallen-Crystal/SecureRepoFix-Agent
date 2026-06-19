import json
from pathlib import Path


def load_timeout(config_path=None):
    path = Path(config_path or "config.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["app"]["timeout"]
