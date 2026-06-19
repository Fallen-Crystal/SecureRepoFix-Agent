from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class AgentAction:
    thought: str
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    finish: bool = False
    final_answer: str = ""


@dataclass
class DiscoveredIssue:
    issue: str
    evidence: str = ""
    likely_files: list[str] = field(default_factory=list)
    confidence: str = "medium"


class AgentModel(Protocol):
    def next_action(self, context: dict[str, Any], history: list[dict[str, Any]]) -> AgentAction:
        """Return the next ReAct action for the current task state."""


class LLMModel:
    """DeepSeek chat model that returns structured ReAct actions."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.0,
    ) -> None:
        self.model = model or os.getenv("REPOFIX_MODEL", "deepseek-v4-flash")
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        self.temperature = temperature

        if not self.api_key:
            raise ValueError("DEEPSEEK_API_KEY is required when using LLMModel")

    def next_action(self, context: dict[str, Any], history: list[dict[str, Any]]) -> AgentAction:
        messages = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": _build_prompt(context, history)},
        ]
        content = self._chat_json(messages)
        return _parse_action(content)

    def discover_issue(
        self,
        *,
        task_id: str,
        test_command: str,
        file_list: str,
        test_output: str,
    ) -> DiscoveredIssue:
        messages = [
            {"role": "system", "content": _issue_discovery_system_prompt()},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task_id": task_id,
                        "test_command": test_command,
                        "file_list": file_list,
                        "test_output": test_output,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ]
        content = self._chat_json(messages)
        return _parse_discovered_issue(content)

    def _chat_json(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM request failed: HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM request failed: {e.reason}") from e

        return body["choices"][0]["message"]["content"]


class ScriptedModel:
    """Offline model used to prove the agent loop without depending on an LLM API."""

    def next_action(self, context: dict[str, Any], history: list[dict[str, Any]]) -> AgentAction:
        if _last_test_passed(history):
            return AgentAction(
                thought="Tests are passing, so the repair loop can stop.",
                finish=True,
                final_answer="All tests passed after the repair.",
            )

        tool_count = sum(1 for step in history if step.get("type") == "tool")

        if tool_count == 0:
            return AgentAction(
                thought="Start by listing files so I know the repository shape before editing.",
                tool="list_files",
            )

        if tool_count == 1:
            keyword = _keyword_from_issue(context["issue"])
            return AgentAction(
                thought=f"Search for the likely symbol `{keyword}` mentioned by the issue.",
                tool="search_code",
                args={"keyword": keyword},
            )

        if tool_count == 2:
            file_path = _first_python_file_from_history(history) or _first_expected_file(context)
            return AgentAction(
                thought=f"Read `{file_path}` to inspect the implementation before making a change.",
                tool="read_file",
                args={"file_path": file_path},
            )

        if tool_count == 3:
            return AgentAction(
                thought="Run the test command once to confirm the failure and collect the error signal.",
                tool="run_tests",
            )

        if _has_failed_tests(history) and not _has_successful_edit(history):
            old_text, new_text = _infer_simple_add_fix(history)
            file_path = (
                _file_path_for_snippet(history, old_text)
                or _file_path_for_snippet(history, "if lowercase or stopwords:")
                or _first_python_file_from_history(history)
                or _first_expected_file(context)
            )
            return AgentAction(
                thought=(
                    "The failure shows string concatenation where numeric addition is expected, "
                    "so replace the implementation with integer conversion."
                ),
                tool="edit_file",
                args={
                    "file_path": file_path,
                    "old_text": old_text,
                    "new_text": new_text,
                },
            )

        if _has_successful_edit(history):
            return AgentAction(
                thought="After changing code, rerun the tests to verify the repair.",
                tool="run_tests",
            )

        return AgentAction(
            thought="No safe next action is available from the current observations.",
            finish=True,
            final_answer="Stopped because the scripted model could not infer a safe repair.",
        )

    def discover_issue(
        self,
        *,
        task_id: str,
        test_command: str,
        file_list: str,
        test_output: str,
    ) -> DiscoveredIssue:
        return discover_issue_from_test_output(
            task_id=task_id,
            test_command=test_command,
            file_list=file_list,
            test_output=test_output,
        )


def discover_issue_from_test_output(
    *,
    task_id: str,
    test_command: str,
    file_list: str,
    test_output: str,
) -> DiscoveredIssue:
    assertion = _first_matching_line(test_output, ["AssertionError", "assert "])
    traceback_file = _first_traceback_file(test_output)
    if assertion:
        issue = (
            f"The configured test command `{test_command}` fails for task `{task_id}`. "
            f"Primary failure signal: {assertion}. Inspect the failing test and relevant source files, "
            "then repair the implementation so the test suite passes."
        )
    else:
        issue = (
            f"The configured test command `{test_command}` fails for task `{task_id}`. "
            "Inspect the test output, identify the failing behavior, and repair the implementation "
            "so the test suite passes."
        )

    likely_files = [traceback_file] if traceback_file else _python_files_from_listing(file_list)[:3]
    return DiscoveredIssue(
        issue=issue,
        evidence=_shorten(test_output, 3000),
        likely_files=likely_files,
        confidence="low" if not assertion else "medium",
    )


def _keyword_from_issue(issue: str) -> str:
    match = re.search(r"[A-Za-z_][A-Za-z0-9_]*", issue)
    return match.group(0) if match else "bug"


def _first_expected_file(context: dict[str, Any]) -> str:
    expected_files = context.get("expected_files") or []
    return expected_files[0] if expected_files else "calculator.py"


def _tool_steps(history: list[dict[str, Any]], tool: str | None = None) -> list[dict[str, Any]]:
    steps = [step for step in history if step.get("type") == "tool"]
    if tool is None:
        return steps
    return [step for step in steps if step.get("tool") == tool]


def _first_python_file_from_history(history: list[dict[str, Any]]) -> str | None:
    for step in _tool_steps(history):
        content = step.get("result", {}).get("content", "")
        match = re.search(r"([A-Za-z0-9_./\\-]+\.py)", content)
        if match:
            return match.group(1).replace("\\", "/")
    return None


def _file_path_for_snippet(history: list[dict[str, Any]], snippet: str) -> str | None:
    for step in _tool_steps(history, "read_file"):
        result = step.get("result", {})
        content = result.get("content", "")
        if snippet in content:
            file_path = result.get("meta", {}).get("file_path")
            if file_path:
                return str(file_path).replace("\\", "/")
    return None


def _has_failed_tests(history: list[dict[str, Any]]) -> bool:
    for step in _tool_steps(history, "run_tests"):
        meta = step.get("result", {}).get("meta", {})
        if meta.get("passed") is False:
            return True
    return False


def _last_test_passed(history: list[dict[str, Any]]) -> bool:
    tests = _tool_steps(history, "run_tests")
    if not tests:
        return False
    return tests[-1].get("result", {}).get("meta", {}).get("passed") is True


def _has_successful_edit(history: list[dict[str, Any]]) -> bool:
    return any(
        step.get("result", {}).get("ok") is True
        for step in _tool_steps(history, "edit_file")
    )


def _infer_simple_add_fix(history: list[dict[str, Any]]) -> tuple[str, str]:
    for step in _tool_steps(history, "read_file"):
        content = step.get("result", {}).get("content", "")
        if "def add(a, b):" in content and "return a + b" in content:
            return (
                "def add(a, b):\n    return a + b\n",
                "def add(a, b):\n    return int(a) + int(b)\n",
            )
        if "def cart_total(items):" in content and 'total += item["price"]' in content:
            return (
                'def cart_total(items):\n    total = 0\n    for item in items:\n        total += item["price"]\n    return total\n',
                'def cart_total(items):\n    total = 0\n    for item in items:\n        total += item["price"] * item["quantity"]\n    return total\n',
            )
        if "if lowercase or stopwords:" in content and "stopwords_lower" in content:
            return (
                "    if lowercase or stopwords:\n        text = text.lower()\n",
                "    if lowercase:\n        text = text.lower()\n",
            )
        if 'text = text.lower()' in content and 'stopwords_lower' in content and 'lowercase or stopwords' in content:
            return (
                "    if lowercase or stopwords:\n        text = text.lower()\n",
                "    if lowercase:\n        text = text.lower()\n",
            )

    return (
        "def add(a, b):\n    return a + b\n",
        "def add(a, b):\n    return int(a) + int(b)\n",
    )


def _issue_discovery_system_prompt() -> str:
    return """You are RepoFix-Agent's issue discovery module.
Infer a repair issue only from the current repository file listing and test output.
Do not claim a fix has already been made.
Respond with one JSON object only, without markdown.

Schema:
{
  "issue": "concise bug report for the repair agent",
  "evidence": "short quote or paraphrase of the failing test signal",
  "likely_files": ["relative/path.py"],
  "confidence": "low | medium | high"
}

The issue should tell the repair agent what behavior is failing, not exactly how
to patch it unless the test output makes that obvious."""


def _system_prompt() -> str:
    return """You are RepoFix-Agent, a code repair agent.
You must choose exactly one next step using the available tools.
Respond with one JSON object only, without markdown.
Memory items are past-run hints only. Never treat memory as proof that the
current repository has already been edited or tested.

Schema:
{
  "thought": "brief reason for the next step",
  "tool": "repo_overview | list_files | search_code | read_file | edit_file | run_tests | get_git_diff",
  "args": {},
  "finish": false,
  "final_answer": ""
}

When the latest test result passes, set finish to true and omit tool or set it to null.
Use edit_file only when you have read enough context and can provide an exact old_text snippet.
Prefer running tests before and after editing.

Tool argument schemas:
- list_files: {"path": ".", "max_depth": 3, "max_entries": 200}
- repo_overview: {"max_depth": 3, "max_files": 120, "max_chars_per_file": 2500}
- search_code: {"keyword": "symbol_or_text", "path": ".", "context_lines": 2}
- read_file: {"file_path": "relative/path.py", "start_line": 1, "end_line": null}
- edit_file: {"file_path": "relative/path.py", "old_text": "exact text", "new_text": "replacement text"}
- run_tests: {}
- get_git_diff: {}"""


def _build_prompt(context: dict[str, Any], history: list[dict[str, Any]]) -> str:
    compact_history = [_compact_step(step) for step in history[-12:]]
    return json.dumps(
        {
            "task": {
                "task_id": context["task_id"],
                "issue": context["issue"],
                "expected_files": context.get("expected_files", []),
                "available_tools": context.get("available_tools", []),
            },
            "memory": context.get("memory", {}),
            "history": compact_history,
            "instruction": "Return the next ReAct action as JSON.",
        },
        ensure_ascii=False,
        indent=2,
    )


def _compact_step(step: dict[str, Any]) -> dict[str, Any]:
    if step.get("type") == "assistant":
        return {
            "type": "assistant",
            "thought": step.get("thought"),
            "tool": step.get("tool"),
            "args": step.get("args"),
            "finish": step.get("finish"),
        }

    result = step.get("result", {})
    return {
        "type": "tool",
        "tool": step.get("tool"),
        "ok": result.get("ok"),
        "error": result.get("error"),
        "meta": result.get("meta", {}),
        "content": _shorten(result.get("content", ""), 5000),
    }


def _shorten(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[truncated to {max_chars} chars]"


def _parse_action(content: str) -> AgentAction:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            raise ValueError(f"LLM did not return JSON: {content}")
        payload = json.loads(match.group(0))

    finish = bool(payload.get("finish", False))
    return AgentAction(
        thought=str(payload.get("thought", "")),
        tool=payload.get("tool"),
        args=payload.get("args") or {},
        finish=finish,
        final_answer=str(payload.get("final_answer", "")),
    )


def _parse_discovered_issue(content: str) -> DiscoveredIssue:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            raise ValueError(f"LLM did not return JSON: {content}")
        payload = json.loads(match.group(0))

    likely_files = payload.get("likely_files") or []
    if not isinstance(likely_files, list):
        likely_files = []

    issue = str(payload.get("issue", "")).strip()
    if not issue:
        issue = "The configured test command fails. Inspect the failure and repair the repository."

    return DiscoveredIssue(
        issue=issue,
        evidence=str(payload.get("evidence", "")),
        likely_files=[str(path) for path in likely_files],
        confidence=str(payload.get("confidence", "medium")),
    )


def _first_matching_line(text: str, needles: list[str]) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if any(needle in stripped for needle in needles):
            return stripped
    return None


def _first_traceback_file(text: str) -> str | None:
    match = re.search(r"([A-Za-z0-9_./\\-]+\.py):\d+", text)
    if not match:
        return None
    return match.group(1).replace("\\", "/")


def _python_files_from_listing(file_list: str) -> list[str]:
    files: list[str] = []
    for line in file_list.splitlines():
        path = line.strip()
        if path.endswith(".py") and not path.startswith("__"):
            files.append(path)
    return files
