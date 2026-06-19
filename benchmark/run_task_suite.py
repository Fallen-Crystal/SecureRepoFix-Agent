from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_ONE = PROJECT_ROOT / "benchmark" / "run_one_task.py"
RESULTS_FILE = PROJECT_ROOT / "benchmark" / "results.jsonl"
SUITE_REPORT_DIR = PROJECT_ROOT / "benchmark" / "reports"
LOCAL_TASKS_FILE = PROJECT_ROOT / "benchmark" / "tasks.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a RepoFix task suite.")
    parser.add_argument(
        "--suite",
        default=str(LOCAL_TASKS_FILE),
        help="JSON file containing local or GitHub repair tasks.",
    )
    parser.add_argument(
        "--model-provider",
        choices=["scripted", "llm"],
        default="llm",
        help="Decision model provider passed to run_one_task.py.",
    )
    parser.add_argument(
        "--memory-provider",
        choices=["jsonl", "taskmemory", "null"],
        default="taskmemory",
        help="Memory backend passed to run_one_task.py.",
    )
    parser.add_argument(
        "--only",
        help="Run only one task_id from the suite.",
    )
    parser.add_argument(
        "--unique-repo-name",
        action="store_true",
        help="Append a timestamp to cloned GitHub repo names to avoid destination collisions.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without running them.",
    )
    parser.add_argument(
        "--report",
        default=str(SUITE_REPORT_DIR / "suite_latest.md"),
        help="Markdown report path for the suite run.",
    )
    return parser.parse_args()


def load_suite(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        tasks = json.load(f)
    if not isinstance(tasks, list):
        raise ValueError("suite file must contain a list of tasks")
    return tasks


def build_command(
    task: dict[str, Any],
    *,
    task_index: int,
    model_provider: str,
    memory_provider: str,
    unique_repo_name: bool,
) -> list[str]:
    if task.get("repo") and not task.get("repo_url") and not task.get("repo_path"):
        return [
            sys.executable,
            str(RUN_ONE),
            str(task_index),
            "--mode",
            "patch",
        ]

    command = [
        sys.executable,
        str(RUN_ONE),
        "--mode",
        "agent",
        "--model-provider",
        model_provider,
        "--memory-provider",
        memory_provider,
        "--test-command",
        str(task.get("test_command") or "pytest"),
        "--max-steps",
        str(task.get("max_steps") or 12),
        "--test-timeout",
        str(task.get("test_timeout") or 30),
        "--setup-timeout",
        str(task.get("setup_timeout") or 180),
    ]

    if task.get("repo_url"):
        command.extend(["--repo-url", str(task["repo_url"])])
        repo_name = task.get("repo_name")
        if unique_repo_name:
            repo_name = f"{task['task_id']}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        if repo_name:
            command.extend(["--repo-name", str(repo_name)])
    elif task.get("repo_path"):
        command.extend(["--repo-path", str(task["repo_path"])])
    else:
        raise ValueError(f"task {task.get('task_id')} must define repo_url or repo_path")

    if task.get("discover_issue", True):
        command.append("--discover-issue")
    elif task.get("issue"):
        command.extend(["--issue", str(task["issue"])])
    else:
        raise ValueError(f"task {task.get('task_id')} needs discover_issue=true or issue")

    setup_commands = []
    if task.get("setup_command"):
        setup_commands.append(str(task["setup_command"]))
    setup_commands.extend(str(command) for command in task.get("setup_commands") or [])
    for setup_command in setup_commands:
        command.extend(["--setup-command", setup_command])

    seed_patches = []
    if task.get("seed_patch"):
        seed_patches.append(task["seed_patch"])
    seed_patches.extend(task.get("seed_patches") or [])
    for seed_patch in seed_patches:
        command.extend(["--seed-patch-json", json.dumps(seed_patch, ensure_ascii=False)])

    return command


def read_result_records() -> list[dict[str, Any]]:
    if not RESULTS_FILE.exists():
        return []
    records: list[dict[str, Any]] = []
    with RESULTS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records


def write_suite_report(records: list[dict[str, Any]], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    passed = sum(1 for record in records if record.get("passed"))
    total = len(records)
    lines = [
        "# RepoFix Suite Report",
        "",
        f"- total: `{total}`",
        f"- passed: `{passed}`",
        f"- failed: `{total - passed}`",
        "",
        "| task_id | passed | before | after | tools | memory | report |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        report_file = record.get("report_file") or ""
        report_cell = f"`{report_file}`" if report_file else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{record.get('task_id')}`",
                    f"`{record.get('passed')}`",
                    f"`{record.get('before_exit_code')}`",
                    f"`{record.get('after_exit_code')}`",
                    f"`{record.get('tool_calls')}`",
                    f"`{record.get('memory_retrieval_count')}`",
                    report_cell,
                ]
            )
            + " |"
        )

    failed_records = [record for record in records if not record.get("passed")]
    if failed_records:
        lines.extend(["", "## Failed Tasks", ""])
        for record in failed_records:
            lines.extend(
                [
                    f"### `{record.get('task_id')}`",
                    "",
                    f"- failure_type: `{record.get('failure_type')}`",
                    f"- issue: {record.get('issue')}",
                    f"- repo: `{record.get('repo')}`",
                    "",
                ]
            )

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    suite_path = Path(args.suite)
    tasks = load_suite(suite_path)

    selected = [
        (index, task)
        for index, task in enumerate(tasks, start=1)
        if (args.only is not None or task.get("enabled", True))
        and (args.only is None or task.get("task_id") == args.only)
    ]
    if not selected:
        raise SystemExit(f"no suite tasks selected from {suite_path}")

    suite_records: list[dict[str, Any]] = []
    for task_index, task in selected:
        task_id = task.get("task_id", "<missing task_id>")
        command = build_command(
            task,
            task_index=task_index,
            model_provider=args.model_provider,
            memory_provider=args.memory_provider,
            unique_repo_name=args.unique_repo_name,
        )
        print("=" * 80)
        print(f"Running suite task: {task_id}")
        print(" ".join(command))
        print("=" * 80)
        if args.dry_run:
            continue

        before_count = len(read_result_records())
        completed = subprocess.run(command, cwd=PROJECT_ROOT)
        new_records = read_result_records()[before_count:]
        if new_records:
            suite_records.append(new_records[-1])
        else:
            suite_records.append(
                {
                    "task_id": task_id,
                    "passed": False,
                    "failure_type": "runner_failed_without_result",
                    "before_exit_code": None,
                    "after_exit_code": None,
                    "tool_calls": None,
                    "memory_retrieval_count": None,
                    "issue": task.get("issue") or "No result record was produced.",
                    "repo": task.get("repo_url") or task.get("repo_path"),
                }
            )
        if completed.returncode != 0:
            write_suite_report(suite_records, Path(args.report))
            print(f"Saved suite report to: {Path(args.report)}")
            raise SystemExit(completed.returncode)

    if not args.dry_run:
        write_suite_report(suite_records, Path(args.report))
        print(f"Saved suite report to: {Path(args.report)}")


if __name__ == "__main__":
    main()
