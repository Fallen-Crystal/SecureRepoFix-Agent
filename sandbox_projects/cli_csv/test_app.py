import subprocess
import sys
from pathlib import Path


def test_cli_prints_numeric_total():
    data_path = Path(__file__).parent / "data.csv"
    result = subprocess.run(
        [sys.executable, "app.py", "--input", str(data_path)],
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.strip() == "total=30"
