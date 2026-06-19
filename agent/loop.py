from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.memory import MemoryAdapter, NullMemoryAdapter
from agent.models import AgentModel
from agent.tool_registry import ToolRegistry


@dataclass
class AgentRunResult:
    task_id: str
    passed: bool
    finished: bool
    steps: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str = ""
    trajectory_file: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    memory_summary: dict[str, Any] = field(default_factory=dict)


class ReActAgent:
    def __init__(
        self,
        model: AgentModel,
        tools: ToolRegistry,
        *,
        max_steps: int = 8,
        memory: MemoryAdapter | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.max_steps = max_steps
        self.memory = memory or NullMemoryAdapter()

    def run(
        self,
        *,
        task_id: str,
        issue: str,
        expected_files: list[str],
        trajectory_dir: Path | None = None,
        initial_history: list[dict[str, Any]] | None = None,
    ) -> AgentRunResult:
        context = {
            "task_id": task_id,
            "issue": issue,
            "expected_files": expected_files,
            "available_tools": self.tools.tool_names,
        }
        history: list[dict[str, Any]] = list(initial_history or [])
        final_answer = ""
        finished = False
        self.memory.write(
            {
                "kind": "goal",
                "task_id": task_id,
                "issue": issue,
                "expected_files": expected_files,
            }
        )

        for step_index in range(1, self.max_steps + 1):
            memory_context = self.memory.retrieve(
                issue,
                {
                    "task_id": task_id,
                    "step": step_index,
                    "history": history[-4:],
                },
            )
            context["memory"] = memory_context
            action = self.model.next_action(context, history)
            if action.finish and not _last_test_passed(history):
                action.thought = (
                    f"{action.thought} Finish was requested before the current "
                    "run had passing tests, so verify the repository state first."
                )
                action.tool = "run_tests"
                action.args = {}
                action.finish = False
                action.final_answer = ""
            elif action.tool == "edit_file" and not _has_test_run(history):
                action.thought = (
                    f"{action.thought} Edit was requested before any current "
                    "test run, so reproduce the failure first."
                )
                action.tool = "run_tests"
                action.args = {}
            history.append(
                {
                    "type": "assistant",
                    "step": step_index,
                    "thought": action.thought,
                    "tool": action.tool,
                    "args": action.args,
                    "finish": action.finish,
                    "final_answer": action.final_answer,
                }
            )

            if action.finish:
                final_answer = action.final_answer
                finished = True
                break

            if not action.tool:
                final_answer = "Model did not provide a tool or finish signal."
                finished = True
                break

            result = self.tools.call(action.tool, action.args)
            history.append(
                {
                    "type": "tool",
                    "step": step_index,
                    "tool": action.tool,
                    "args": action.args,
                    "result": result,
                }
            )
            self.memory.write(
                {
                    "kind": "episodic_event",
                    "task_id": task_id,
                    "step": step_index,
                    "tool": action.tool,
                    "args": action.args,
                    "ok": result.get("ok"),
                    "error": result.get("error"),
                    "meta": result.get("meta", {}),
                    "content": _shorten_text(result.get("content", ""), 2000),
                }
            )

            if action.tool == "run_tests":
                passed = result.get("meta", {}).get("passed")
                if passed is False:
                    self.memory.write(
                        {
                            "kind": "reflection",
                            "task_id": task_id,
                            "step": step_index,
                            "trigger": "pytest_failed",
                            "failure_summary": _summarize_test_failure(result),
                            "lesson": "The current repair hypothesis is not verified; use the concrete failing assertion and traceback before editing.",
                            "next_strategy": "Read the failing test and the implicated source file, then make the smallest code change that explains the assertion.",
                            "summary": "Test command failed; use the failure output to revise the repair plan.",
                            "test_meta": result.get("meta", {}),
                        }
                    )
                elif passed is True:
                    self.memory.write(
                        {
                            "kind": "skill",
                            "task_id": task_id,
                            "step": step_index,
                            "skill_name": "test_verified_repository_repair",
                            "applicable_when": [
                                "a repository has a reproducible failing test",
                                "the fix is validated by rerunning the configured test command",
                            ],
                            "repair_pattern": _summarize_successful_repair(history),
                            "source_task": task_id,
                            "summary": "A code change was verified by the configured test command.",
                        }
                    )
                    finished = True
                    final_answer = "All tests passed after the repair."
                    break

        passed = _last_test_passed(history)
        metrics = {
            **self.tools.metrics(),
            "steps": _assistant_step_count(history),
            "memory_retrieval_count": self.memory.retrieval_count,
            "memory_write_count": self.memory.write_count,
            "failure_type": None if passed else _failure_type(history, finished),
            "constraint_violation": _has_constraint_violation(history),
        }
        memory_summary = self.memory.summarize(task_id)
        trajectory_file = None
        if trajectory_dir is not None:
            trajectory_file = str(
                _save_trajectory(
                    trajectory_dir,
                    context,
                    history,
                    passed,
                    metrics=metrics,
                    memory_summary=memory_summary,
                )
            )

        return AgentRunResult(
            task_id=task_id,
            passed=passed,
            finished=finished,
            steps=history,
            final_answer=final_answer,
            trajectory_file=trajectory_file,
            metrics=metrics,
            memory_summary=memory_summary,
        )


def _last_test_passed(history: list[dict[str, Any]]) -> bool:
    test_steps = [
        step for step in history
        if step.get("type") == "tool" and step.get("tool") == "run_tests"
    ]
    if not test_steps:
        return False
    return test_steps[-1].get("result", {}).get("meta", {}).get("passed") is True


def _has_test_run(history: list[dict[str, Any]]) -> bool:
    return any(
        step.get("type") == "tool" and step.get("tool") == "run_tests"
        for step in history
    )


def _assistant_step_count(history: list[dict[str, Any]]) -> int:
    return sum(1 for step in history if step.get("type") == "assistant")


def _has_constraint_violation(history: list[dict[str, Any]]) -> bool:
    return any(
        step.get("type") == "tool"
        and step.get("result", {}).get("meta", {}).get("risk") == "dangerous"
        for step in history
    )


def _failure_type(history: list[dict[str, Any]], finished: bool) -> str:
    if _has_constraint_violation(history):
        return "constraint_violation"
    if any(
        step.get("type") == "tool" and step.get("result", {}).get("ok") is False
        for step in history
    ):
        return "tool_error"
    if not finished:
        return "max_steps"
    return "tests_failed"


def _summarize_test_failure(result: dict[str, Any]) -> str:
    content = result.get("content", "")
    if not content:
        return str(result.get("error") or result.get("meta") or "test command failed")

    useful_lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if (
            "FAILED " in stripped
            or "AssertionError" in stripped
            or stripped.startswith("E ")
            or stripped.startswith(">")
            or "assert " in stripped
            or "Error" in stripped
        ):
            useful_lines.append(stripped)
        if len(useful_lines) >= 8:
            break

    if not useful_lines:
        useful_lines = content.splitlines()[:8]
    return _shorten_text("\n".join(useful_lines), 1200)


def _summarize_successful_repair(history: list[dict[str, Any]]) -> str:
    edit_steps = [
        step for step in history
        if step.get("type") == "tool" and step.get("tool") == "edit_file"
    ]
    if not edit_steps:
        return "Run the configured test command and stop only after it passes."

    last_edit = edit_steps[-1]
    args = last_edit.get("args", {})
    file_path = args.get("file_path", "unknown file")
    old_text = _shorten_text(str(args.get("old_text", "")).strip(), 300)
    new_text = _shorten_text(str(args.get("new_text", "")).strip(), 300)
    return (
        f"Edited {file_path} and verified with tests. "
        f"Replace `{old_text}` with `{new_text}` when the failing assertion matches this behavior."
    )


def _shorten_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[truncated to {max_chars} chars]"


def _save_trajectory(
    trajectory_dir: Path,
    context: dict[str, Any],
    history: list[dict[str, Any]],
    passed: bool,
    *,
    metrics: dict[str, Any],
    memory_summary: dict[str, Any],
) -> Path:
    trajectory_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    task_id = context["task_id"]
    path = trajectory_dir / f"{task_id}-{timestamp}.json"
    payload = {
        "context": context,
        "passed": passed,
        "metrics": metrics,
        "memory_summary": memory_summary,
        "steps": history,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
