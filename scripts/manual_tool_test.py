import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.tool_registry import ToolRegistry


REPO_ROOT = PROJECT_ROOT / "sandbox_projects" / "calculator"
AUDIT_LOG = PROJECT_ROOT / "logs" / "audit.jsonl"
CALCULATOR_FILE = REPO_ROOT / "calculator.py"
BUGGY_ADD = "def add(a, b):\n    return a + b\n"
FIXED_ADD = "def add(a, b):\n    return int(a) + int(b)\n"


def show(result):
    print("=" * 80)
    print(f"tool: {result['tool']}")
    print(f"ok: {result['ok']}")
    print(f"error: {result['error']}")
    print("-" * 80)
    print(result["content"])
    print("=" * 80)


def main():
    CALCULATOR_FILE.write_text(BUGGY_ADD, encoding="utf-8")
    tools = ToolRegistry(
        repo_root=REPO_ROOT,
        project_root=PROJECT_ROOT,
        test_command="pytest",
        task_id="manual_calc",
        audit_log_path=AUDIT_LOG,
    )

    show(tools.call("list_files"))

    show(tools.call("read_file", {"file_path": "calculator.py"}))

    show(tools.call("search_code", {"keyword": "add"}))

    show(tools.call("run_tests"))

    show(tools.call(
        "edit_file",
        {
            "file_path": "calculator.py",
            "old_text": BUGGY_ADD,
            "new_text": FIXED_ADD,
        },
    ))

    show(tools.call("run_tests"))

    show(tools.call("get_git_diff"))


if __name__ == "__main__":
    main()
