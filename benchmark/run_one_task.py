from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

RESULTS_FILE = PROJECT_ROOT / "benchmark" / "results.jsonl"
TRAJECTORY_DIR = PROJECT_ROOT / "benchmark" / "trajectories"
IMPORTED_REPOS_DIR = PROJECT_ROOT / "sandbox_projects" / "imported"
LOG_DIR = PROJECT_ROOT / "logs"
AUDIT_LOG_FILE = LOG_DIR / "audit.jsonl"
MEMORY_FILE = LOG_DIR / "memory.jsonl"
TASKMEMORY_FILE = LOG_DIR / "taskmemory.memories.jsonl"
TASKMEMORY_AUDIT_FILE = LOG_DIR / "taskmemory.audit.jsonl"
DEFAULT_TASKMEMORY_PATH = PROJECT_ROOT.parent / "TaskMemory-Agent"
REPORT_DIR = PROJECT_ROOT / "benchmark" / "reports"

BLOCKED_SETUP_PATTERNS = (
    r"\brm\b",
    r"\bdel\b",
    r"\brmdir\b",
    r"\bcurl\b",
    r"\bwget\b",
    r"\bscp\b",
    r"\bssh\b",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bformat\b",
    r"\bshutdown\b",
)

from agent import LLMModel, ReActAgent, ScriptedModel, build_memory_adapter
from agent.tool_registry import ToolRegistry
from tools.git_tools import clone_repository


TASKS_FILE = PROJECT_ROOT / "benchmark" / "tasks.json"


def show(title: str, result: dict) -> None:
    print("=" * 80)
    print(title)
    print(f"tool: {result['tool']}")
    print(f"ok: {result['ok']}")
    print(f"error: {result['error']}")
    print("-" * 80)
    print(result["content"])
    print("=" * 80)


def load_tasks() -> list[dict]:
    if not TASKS_FILE.exists():
        raise FileNotFoundError(f"tasks file not found: {TASKS_FILE}")

    with TASKS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def select_task(tasks: list[dict], selector: str | None) -> dict:
    if not tasks:
        raise ValueError("tasks file is empty")

    if selector is None:
        return tasks[0]

    for task in tasks:
        if task.get("task_id") == selector:
            return task

    if selector.isdigit():
        index = int(selector) - 1
        if 0 <= index < len(tasks):
            return tasks[index]

    available = ", ".join(
        f"{i:02d}:{task.get('task_id', '<missing task_id>')}"
        for i, task in enumerate(tasks, start=1)
    )
    raise ValueError(f"task not found: {selector}. Available tasks: {available}")


def save_result(record: dict) -> None:
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    with RESULTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def reset_task_files(repo_root: Path, task: dict) -> None:
    buggy_files = task.get("buggy_files", {})

    if not buggy_files:
        return

    for relative_path, content in buggy_files.items():
        file_path = repo_root / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

        print(f"[reset] wrote buggy file: {file_path}")


def preview_reset_files(repo_root: Path, task: dict) -> None:
    buggy_files = task.get("buggy_files", {})
    if not buggy_files:
        return

    print("[check after reset]")
    for relative_path in buggy_files:
        print(f"--- {relative_path} ---")
        print((repo_root / relative_path).read_text(encoding="utf-8"))


def _is_blocked_setup_command(command: str) -> str | None:
    normalized = " ".join(command.strip().split())
    if not normalized:
        return "setup command is empty"

    for pattern in BLOCKED_SETUP_PATTERNS:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return f"dangerous setup command blocked: {normalized}"

    return None


def run_setup_commands(
    repo_root: Path,
    commands: list[str],
    *,
    timeout: int,
) -> list[dict]:
    results: list[dict] = []
    if not commands:
        return results

    print("[setup] running repository setup commands")
    for command in commands:
        print(f"[setup] $ {command}")
        blocked_reason = _is_blocked_setup_command(command)
        if blocked_reason:
            print(f"[setup] blocked: {blocked_reason}")
            results.append(
                {
                    "command": command,
                    "ok": False,
                    "exit_code": None,
                    "error": blocked_reason,
                    "stdout": "",
                    "stderr": "",
                }
            )
            break

        try:
            completed = subprocess.run(
                command,
                cwd=repo_root,
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            ok = completed.returncode == 0
            print(f"[setup] exit_code: {completed.returncode}")
            results.append(
                {
                    "command": command,
                    "ok": ok,
                    "exit_code": completed.returncode,
                    "error": None if ok else "setup command failed",
                    "stdout": completed.stdout[-4000:],
                    "stderr": completed.stderr[-4000:],
                }
            )
            if not ok:
                break
        except subprocess.TimeoutExpired as e:
            stdout = e.stdout or ""
            stderr = e.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="ignore")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="ignore")
            print(f"[setup] timed out after {timeout}s")
            results.append(
                {
                    "command": command,
                    "ok": False,
                    "exit_code": None,
                    "error": f"setup command timed out after {timeout}s",
                    "stdout": stdout[-4000:],
                    "stderr": stderr[-4000:],
                }
            )
            break

    return results


def apply_seed_patches(repo_root: Path, task: dict) -> list[dict]:
    patches = list(task.get("seed_patches") or [])
    if task.get("seed_patch"):
        patches.append(task["seed_patch"])

    results: list[dict] = []
    if not patches:
        return results

    print("[seed] applying local seed patches")
    for patch in patches:
        file_path = str(patch.get("file_path") or "")
        old_text = str(patch.get("old_text") or "")
        new_text = str(patch.get("new_text") or "")
        result = {
            "file_path": file_path,
            "ok": False,
            "error": None,
            "old_chars": len(old_text),
            "new_chars": len(new_text),
        }

        try:
            target = (repo_root / file_path).resolve()
            target.relative_to(repo_root.resolve())
            if not target.is_file():
                result["error"] = f"seed patch file not found: {file_path}"
                results.append(result)
                break

            text = target.read_text(encoding="utf-8")
            count = text.count(old_text)
            if count != 1:
                result["error"] = (
                    f"seed old_text must appear exactly once in {file_path}; found {count}"
                )
                results.append(result)
                break

            target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
            result["ok"] = True
            print(f"[seed] patched {file_path}")
            results.append(result)
        except Exception as e:
            result["error"] = str(e)
            results.append(result)
            break

    return results


def seed_failed(seed_results: list[dict]) -> bool:
    return any(not result.get("ok") for result in seed_results)


def setup_failed(setup_results: list[dict]) -> bool:
    return any(not result.get("ok") for result in setup_results)


def _shorten_report_text(text: str | None, max_chars: int = 3000) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[truncated to {max_chars} chars]"


def _safe_report_name(task_id: str, timestamp: str) -> str:
    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id)
    safe_timestamp = re.sub(r"[^0-9T-]+", "", timestamp)
    return f"{safe_timestamp}_{safe_task_id}.md"


def write_run_report(
    *,
    record: dict,
    run_result: dict,
    setup_results: list[dict],
    report_dir: Path,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / _safe_report_name(record["task_id"], record["timestamp"])

    lines = [
        "# RepoFix Run Report",
        "",
        f"- task_id: `{record['task_id']}`",
        f"- timestamp: `{record['timestamp']}`",
        f"- repo: `{record['repo']}`",
        f"- mode: `{record['mode']}`",
        f"- passed: `{record['passed']}`",
        f"- before_exit_code: `{record.get('before_exit_code')}`",
        f"- after_exit_code: `{record.get('after_exit_code')}`",
        f"- test_command: `{record['test_command']}`",
        f"- test_timeout: `{record.get('test_timeout')}`",
        f"- memory_provider: `{record.get('memory_provider')}`",
        "",
        "## Issue",
        "",
        record.get("issue") or "",
    ]

    if setup_results:
        lines.extend(["", "## Setup", ""])
        for result in setup_results:
            lines.extend(
                [
                    f"### `{result['command']}`",
                    "",
                    f"- ok: `{result.get('ok')}`",
                    f"- exit_code: `{result.get('exit_code')}`",
                    f"- error: `{result.get('error')}`",
                ]
            )

    seed_results = record.get("seed_results") or []
    if seed_results:
        lines.extend(["", "## Seed Patches", ""])
        for result in seed_results:
            lines.extend(
                [
                    f"### `{result.get('file_path')}`",
                    "",
                    f"- ok: `{result.get('ok')}`",
                    f"- error: `{result.get('error')}`",
                ]
            )

    metrics = {key: record.get(key) for key in (
        "tool_calls",
        "edit_count",
        "blocked_tool_calls",
        "approval_count",
        "memory_retrieval_count",
        "memory_write_count",
        "failure_type",
        "constraint_violation",
    ) if key in record}
    lines.extend(["", "## Metrics", "", "```json"])
    lines.append(json.dumps(metrics, ensure_ascii=False, indent=2))
    lines.append("```")

    if record.get("trajectory_file"):
        lines.extend(["", "## Trajectory", "", f"`{record['trajectory_file']}`"])

    for title, test_key in (("Before Test Output", "before_test"), ("After Test Output", "after_test")):
        result = run_result.get(test_key) or {}
        content = _shorten_report_text(result.get("content"))
        if content:
            lines.extend(["", f"## {title}", "", "```text", content, "```"])

    report_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one RepoFix benchmark task."
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="Task id or 1-based task number, for example calc_001, 1, or 03.",
    )
    parser.add_argument(
        "--mode",
        choices=["patch", "agent"],
        default="patch",
        help="patch uses the expected patch from tasks.json; agent runs the ReAct loop.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=8,
        help="Maximum ReAct steps when --mode agent is used.",
    )
    parser.add_argument(
        "--model-provider",
        choices=["scripted", "llm"],
        default="scripted",
        help="Decision model for --mode agent. scripted is offline; llm uses DEEPSEEK_API_KEY.",
    )
    parser.add_argument(
        "--model",
        help="Model name for --model-provider llm. Defaults to REPOFIX_MODEL or deepseek-v4-flash.",
    )
    parser.add_argument(
        "--repo-url",
        help="GitHub repository URL to clone into sandbox_projects/imported before running.",
    )
    parser.add_argument(
        "--repo-name",
        help="Optional local folder name for --repo-url imports.",
    )
    parser.add_argument(
        "--repo-path",
        help="Existing local repository path to run against instead of a benchmark task.",
    )
    parser.add_argument(
        "--issue",
        help="Bug description for --repo-url or --repo-path runs. Omit when using --discover-issue.",
    )
    parser.add_argument(
        "--discover-issue",
        action="store_true",
        help="Run tests first, infer the issue from failures, then feed that issue to the agent.",
    )
    parser.add_argument(
        "--test-command",
        default="pytest",
        help="Test command for --repo-url or --repo-path runs.",
    )
    parser.add_argument(
        "--test-timeout",
        type=int,
        default=30,
        help="Timeout in seconds for each configured test command.",
    )
    parser.add_argument(
        "--setup-command",
        action="append",
        default=[],
        help=(
            "Repository setup command to run before discovery/repair. "
            "May be provided multiple times."
        ),
    )
    parser.add_argument(
        "--seed-patch-json",
        action="append",
        default=[],
        help=(
            "JSON object with file_path, old_text, and new_text to apply after "
            "clone/path resolution and before setup. May be provided multiple times."
        ),
    )
    parser.add_argument(
        "--setup-timeout",
        type=int,
        default=180,
        help="Timeout in seconds for each setup command.",
    )
    parser.add_argument(
        "--approval-mode",
        choices=["auto", "prompt", "deny"],
        default="auto",
        help="Approval policy for write-risk tool calls.",
    )
    parser.add_argument(
        "--memory-file",
        default=str(MEMORY_FILE),
        help="Memory JSONL file used by --memory-provider jsonl or taskmemory.",
    )
    parser.add_argument(
        "--memory-provider",
        choices=["jsonl", "taskmemory", "null"],
        default="jsonl",
        help="Memory backend for agent runs.",
    )
    parser.add_argument(
        "--taskmemory-path",
        default=str(DEFAULT_TASKMEMORY_PATH),
        help="Path to sibling TaskMemory-Agent project when --memory-provider taskmemory is used.",
    )
    parser.add_argument(
        "--taskmemory-audit-file",
        default=str(TASKMEMORY_AUDIT_FILE),
        help="Audit JSONL file for TaskMemory-Agent storage.",
    )
    parser.add_argument(
        "--report-dir",
        default=str(REPORT_DIR),
        help="Directory for per-run Markdown reports.",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Disable per-run Markdown report generation.",
    )
    return parser.parse_args()


def build_external_task(args: argparse.Namespace) -> dict | None:
    if not args.repo_url and not args.repo_path:
        return None

    if not args.issue and not args.discover_issue:
        print(
            "error: --issue is required for external repos unless --discover-issue is used",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if args.repo_url:
        clone_result = clone_repository(
            args.repo_url,
            IMPORTED_REPOS_DIR,
            name=args.repo_name,
        )
        if not clone_result["ok"]:
            print(f"error: {clone_result['error']}", file=sys.stderr)
            raise SystemExit(2)
        repo_root = Path(clone_result["meta"]["repo_path"])
        task_id = f"github_{repo_root.name}"
    else:
        repo_root = Path(args.repo_path).resolve()
        if not repo_root.exists() or not repo_root.is_dir():
            print(f"error: repo path not found: {repo_root}", file=sys.stderr)
            raise SystemExit(2)
        task_id = f"local_{repo_root.name}"

    seed_patches = []
    for raw_patch in args.seed_patch_json:
        try:
            patch = json.loads(raw_patch)
        except json.JSONDecodeError as e:
            print(f"error: invalid --seed-patch-json: {e}", file=sys.stderr)
            raise SystemExit(2) from None
        if not isinstance(patch, dict):
            print("error: --seed-patch-json must decode to an object", file=sys.stderr)
            raise SystemExit(2)
        seed_patches.append(patch)

    return {
        "task_id": task_id,
        "repo": str(repo_root),
        "issue": args.issue or "Discover the failing behavior by running the configured tests.",
        "test_command": args.test_command,
        "setup_commands": [],
        "seed_patches": seed_patches,
        "expected_files": [],
        "external": True,
        "discover_issue": args.discover_issue,
    }


def resolve_task_and_repo(args: argparse.Namespace) -> tuple[dict, Path]:
    external_task = build_external_task(args)
    if external_task is not None:
        return external_task, Path(external_task["repo"]).resolve()

    tasks = load_tasks()
    try:
        task = select_task(tasks, args.task)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2) from None

    return task, PROJECT_ROOT / task["repo"]


def run_patch_mode(
    task: dict,
    repo_root: Path,
    test_command: str,
    test_timeout: int,
    approval_mode: str,
) -> dict:
    tools = ToolRegistry(
        repo_root=repo_root,
        project_root=PROJECT_ROOT,
        test_command=test_command,
        task_id=task["task_id"],
        approval_mode=approval_mode,
        audit_log_path=AUDIT_LOG_FILE,
        test_timeout=test_timeout,
    )
    show("Step 1: list files", tools.call("list_files"))

    for keyword in task.get("search_keywords", []):
        show(f"Step 2: search keyword {keyword}", tools.call("search_code", {"keyword": keyword}))

    files_to_read = task.get("expected_files", [])
    if not files_to_read and task.get("patch"):
        files_to_read = [task["patch"]["file_path"]]

    for file_path in files_to_read:
        show(f"Step 3: read {file_path}", tools.call("read_file", {"file_path": file_path}))

    before_test = tools.call("run_tests")
    show("Step 4: run tests before fix", before_test)

    patch = task.get("patch")
    if not patch:
        return {
            "before_test": before_test,
            "after_test": before_test,
            "passed": False,
            "edit_ok": False,
            "patch_file": None,
            "trajectory_file": None,
            "metrics": tools.metrics(),
        }

    edit_result = tools.call(
        "edit_file",
        {
            "file_path": patch["file_path"],
            "old_text": patch["old_text"],
            "new_text": patch["new_text"],
        },
    )
    show("Step 5: edit file", edit_result)

    after_test = tools.call("run_tests")
    show("Step 6: run tests after fix", after_test)
    show("Step 7: git diff", tools.call("get_git_diff"))

    return {
        "before_test": before_test,
        "after_test": after_test,
        "passed": after_test["meta"].get("passed", False),
        "edit_ok": edit_result["ok"],
        "patch_file": patch["file_path"],
        "trajectory_file": None,
        "metrics": {
            **tools.metrics(),
            "failure_type": None if after_test["meta"].get("passed", False) else "tests_failed",
            "memory_retrieval_count": 0,
            "constraint_violation": tools.metrics()["blocked_tool_calls"] > 0,
        },
    }


def discover_issue_for_run(
    *,
    tools: ToolRegistry,
    model: LLMModel | ScriptedModel,
    task_id: str,
    test_command: str,
) -> dict:
    print("[issue discovery] listing repository files")
    file_result = tools.call("list_files")
    print("[issue discovery] running tests to discover current failure")
    test_result = tools.call("run_tests")

    initial_history = [
        {
            "type": "tool",
            "step": 0,
            "tool": "list_files",
            "args": {},
            "result": file_result,
        },
        {
            "type": "tool",
            "step": 0,
            "tool": "run_tests",
            "args": {},
            "result": test_result,
        },
    ]

    if test_result["meta"].get("passed") is True:
        issue = "No failing tests were discovered by the configured test command."
        print(f"[issue discovery] {issue}")
        return {
            "issue": issue,
            "evidence": test_result.get("content", ""),
            "likely_files": [],
            "initial_history": initial_history,
            "test_result": test_result,
            "tests_passed": True,
        }

    discovered = model.discover_issue(
        task_id=task_id,
        test_command=test_command,
        file_list=file_result.get("content", ""),
        test_output=test_result.get("content", ""),
    )
    print("[issue discovery] inferred issue:")
    print(discovered.issue)
    if discovered.likely_files:
        print(f"[issue discovery] likely files: {', '.join(discovered.likely_files)}")

    return {
        "issue": discovered.issue,
        "evidence": discovered.evidence,
        "likely_files": discovered.likely_files,
        "initial_history": initial_history,
        "test_result": test_result,
        "tests_passed": False,
    }


def run_agent_mode(
    task: dict,
    repo_root: Path,
    test_command: str,
    test_timeout: int,
    max_steps: int,
    model_provider: str,
    model_name: str | None,
    approval_mode: str,
    memory_file: Path,
    memory_provider: str = "jsonl",
    taskmemory_path: Path | None = None,
    taskmemory_audit_file: Path | None = None,
    discover_issue: bool = False,
) -> dict:
    diff_root = repo_root if (repo_root / ".git").exists() else PROJECT_ROOT
    tools = ToolRegistry(
        repo_root=repo_root,
        project_root=diff_root,
        test_command=test_command,
        task_id=task["task_id"],
        approval_mode=approval_mode,
        audit_log_path=AUDIT_LOG_FILE,
        test_timeout=test_timeout,
    )
    model = LLMModel(model=model_name) if model_provider == "llm" else ScriptedModel()
    initial_history: list[dict] = []
    discovered_issue = None

    if discover_issue:
        discovery = discover_issue_for_run(
            tools=tools,
            model=model,
            task_id=task["task_id"],
            test_command=test_command,
        )
        initial_history = discovery["initial_history"]
        discovered_issue = discovery["issue"]
        task["issue"] = discovered_issue
        task["expected_files"] = discovery["likely_files"]

        if discovery["tests_passed"]:
            show("Final git diff", tools.call("get_git_diff"))
            return {
                "before_test": discovery["test_result"],
                "after_test": discovery["test_result"],
                "passed": True,
                "edit_ok": False,
                "patch_file": None,
                "trajectory_file": None,
                "metrics": {
                    **tools.metrics(),
                    "steps": 0,
                    "memory_retrieval_count": 0,
                    "memory_write_count": 0,
                    "failure_type": None,
                    "constraint_violation": False,
                },
                "memory_summary": {},
                "discovered_issue": discovered_issue,
                "discovery_evidence": discovery["evidence"],
            }

    agent = ReActAgent(
        model=model,
        tools=tools,
        max_steps=max_steps,
        memory=build_memory_adapter(
            memory_provider,
            memory_file=memory_file,
            taskmemory_path=taskmemory_path,
            taskmemory_audit_file=taskmemory_audit_file,
        ),
    )
    result = agent.run(
        task_id=task["task_id"],
        issue=task["issue"],
        expected_files=task.get("expected_files", []),
        trajectory_dir=TRAJECTORY_DIR,
        initial_history=initial_history,
    )

    for step in result.steps:
        if step["type"] == "assistant":
            print("=" * 80)
            print(f"Agent Step {step['step']}: thought")
            print("-" * 80)
            print(step["thought"])
            if step.get("tool"):
                print(f"action: {step['tool']} {step.get('args', {})}")
            if step.get("finish"):
                print(f"finish: {step.get('final_answer', '')}")
            print("=" * 80)
        else:
            show(f"Agent Step {step['step']}: observation", step["result"])

    show("Final git diff", tools.call("get_git_diff"))

    test_steps = [
        step for step in result.steps
        if step.get("type") == "tool" and step.get("tool") == "run_tests"
    ]
    before_test = test_steps[0]["result"] if test_steps else {"meta": {}}
    after_test = test_steps[-1]["result"] if test_steps else {"meta": {}}
    edit_steps = [
        step for step in result.steps
        if step.get("type") == "tool" and step.get("tool") == "edit_file"
    ]
    edit_ok = any(step["result"].get("ok") for step in edit_steps)

    return {
        "before_test": before_test,
        "after_test": after_test,
        "passed": result.passed,
        "edit_ok": edit_ok,
        "patch_file": None,
        "trajectory_file": result.trajectory_file,
        "metrics": result.metrics,
        "memory_summary": result.memory_summary,
        "discovered_issue": discovered_issue,
    }


def main() -> None:
    args = parse_args()
    if args.memory_provider == "taskmemory" and Path(args.memory_file) == MEMORY_FILE:
        args.memory_file = str(TASKMEMORY_FILE)

    task, repo_root = resolve_task_and_repo(args)

    task_id = task["task_id"]
    issue = task["issue"]
    test_command = task.get("test_command", "pytest")
    test_timeout = int(task.get("test_timeout") or args.test_timeout)
    setup_commands = list(task.get("setup_commands") or [])
    if task.get("setup_command"):
        setup_commands.append(str(task["setup_command"]))
    if args.setup_command:
        setup_commands.extend(args.setup_command)

    reset_task_files(repo_root, task)
    preview_reset_files(repo_root, task)
    seed_results = apply_seed_patches(repo_root, task)
    setup_results: list[dict] = []
    if not seed_failed(seed_results):
        setup_results = run_setup_commands(
            repo_root,
            setup_commands,
            timeout=args.setup_timeout,
        )


    print(f"Running task: {task_id}")
    print(f"Mode: {args.mode}")
    if args.mode == "agent":
        print(f"Model provider: {args.model_provider}")
        print(f"Memory provider: {args.memory_provider}")
        print(f"Discover issue: {args.discover_issue}")
    print(f"Issue: {issue}")
    print(f"Repo: {repo_root}")
    print(f"Test command: {test_command}")
    print(f"Test timeout: {test_timeout}s")
    print()

    if seed_failed(seed_results) or setup_failed(setup_results):
        failure_type = "seed_failed" if seed_failed(seed_results) else "setup_failed"
        before_test = {"meta": {}}
        after_test = {"meta": {}}
        timestamp = datetime.now().isoformat(timespec="seconds")
        result_record = {
            "timestamp": timestamp,
            "task_id": task_id,
            "repo": str(repo_root),
            "issue": issue,
            "mode": args.mode,
            "model_provider": args.model_provider,
            "memory_provider": args.memory_provider,
            "test_command": test_command,
            "test_timeout": test_timeout,
            "setup_commands": setup_commands,
            "setup_results": setup_results,
            "seed_results": seed_results,
            "before_exit_code": None,
            "after_exit_code": None,
            "passed": False,
            "edit_ok": False,
            "expected_files": task.get("expected_files", []),
            "patch_file": None,
            "trajectory_file": None,
            "failure_type": failure_type,
        }
        run_result = {
            "before_test": before_test,
            "after_test": after_test,
            "passed": False,
            "edit_ok": False,
            "patch_file": None,
            "trajectory_file": None,
            "metrics": {"failure_type": failure_type},
        }
        if not args.no_report:
            report_file = write_run_report(
                record=result_record,
                run_result=run_result,
                setup_results=setup_results,
                report_dir=Path(args.report_dir),
            )
            result_record["report_file"] = str(report_file)
            print(f"Saved run report to: {report_file}")
        save_result(result_record)
        print(f"Saved result to: {RESULTS_FILE}")
        raise SystemExit(2)

    if task.get("external") and args.mode != "agent":
        print("error: external repositories require --mode agent", file=sys.stderr)
        raise SystemExit(2)

    if args.mode == "agent":
        try:
            run_result = run_agent_mode(
                task,
                repo_root,
                test_command,
                test_timeout,
                args.max_steps,
                args.model_provider,
                args.model,
                args.approval_mode,
                Path(args.memory_file),
                args.memory_provider,
                Path(args.taskmemory_path),
                Path(args.taskmemory_audit_file),
                args.discover_issue or task.get("discover_issue", False),
            )
        except (RuntimeError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            raise SystemExit(2) from None
    else:
        run_result = run_patch_mode(
            task,
            repo_root,
            test_command,
            test_timeout,
            args.approval_mode,
        )

    before_test = run_result["before_test"]
    after_test = run_result["after_test"]
    passed = run_result["passed"]
    issue = run_result.get("discovered_issue") or issue

    print("\nTask Summary")
    print("-" * 80)
    print(f"task_id: {task_id}")
    print(f"mode: {args.mode}")
    if run_result.get("discovered_issue"):
        print(f"discovered_issue: {run_result['discovered_issue']}")
    print(f"passed: {passed}")
    print(f"before_exit_code: {before_test['meta'].get('exit_code')}")
    print(f"after_exit_code: {after_test['meta'].get('exit_code')}")
    if run_result.get("trajectory_file"):
        print(f"trajectory_file: {run_result['trajectory_file']}")
    if run_result.get("metrics"):
        print(f"tool_calls: {run_result['metrics'].get('tool_calls')}")
        print(f"blocked_tool_calls: {run_result['metrics'].get('blocked_tool_calls')}")
        print(f"memory_retrieval_count: {run_result['metrics'].get('memory_retrieval_count')}")
    print("-" * 80)


    result_record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "task_id": task_id,
        "repo": str(repo_root),
        "issue": issue,
        "mode": args.mode,
        "model_provider": args.model_provider,
        "memory_provider": args.memory_provider,
        "test_command": test_command,
        "test_timeout": test_timeout,
        "setup_commands": setup_commands,
        "setup_results": setup_results,
        "seed_results": seed_results,
        "before_exit_code": before_test["meta"].get("exit_code"),
        "after_exit_code": after_test["meta"].get("exit_code"),
        "passed": passed,
        "edit_ok": run_result["edit_ok"],
        "expected_files": task.get("expected_files", []),
        "patch_file": run_result["patch_file"],
        "trajectory_file": run_result["trajectory_file"],
        **(run_result.get("metrics") or {}),
    }

    if not args.no_report:
        report_file = write_run_report(
            record=result_record,
            run_result=run_result,
            setup_results=setup_results,
            report_dir=Path(args.report_dir),
        )
        result_record["report_file"] = str(report_file)
        print(f"\nSaved run report to: {report_file}")

    save_result(result_record)

    print(f"\nSaved result to: {RESULTS_FILE}")



if __name__ == "__main__":
    main()
