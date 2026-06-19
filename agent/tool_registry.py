from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from agent.firewall import (
    DEFAULT_POLICY,
    AuditLogger,
    FirewallDecision,
    RiskLevel,
    ToolFirewall,
    ToolPolicy,
)
from tools.basic_tools import (
    edit_file,
    get_git_diff,
    list_files,
    read_file,
    repo_overview,
    run_tests,
    search_code,
    tool_result,
)


class ToolRegistry:
    def __init__(
        self,
        repo_root: Path,
        project_root: Path,
        test_command: str,
        *,
        task_id: str | None = None,
        approval_mode: str = "auto",
        audit_log_path: Path | None = None,
        test_timeout: int = 30,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.project_root = project_root.resolve()
        self.test_command = test_command
        self.test_timeout = test_timeout
        self.task_id = task_id
        policy = dict(DEFAULT_POLICY)
        run_tests_policy = policy["run_tests"]
        policy["run_tests"] = ToolPolicy(
            run_tests_policy.risk,
            approval_required=run_tests_policy.approval_required,
            path_required=run_tests_policy.path_required,
            allowed_extensions=run_tests_policy.allowed_extensions,
            allowed_commands=run_tests_policy.allowed_commands,
            timeout=test_timeout,
        )
        self.firewall = ToolFirewall(
            self.repo_root,
            policy=policy,
            configured_test_command=test_command,
            approval_mode=approval_mode,
        )
        self.audit_logger = AuditLogger(audit_log_path or self.project_root / "logs" / "audit.jsonl")
        self.call_count = 0
        self.blocked_call_count = 0
        self.approval_count = 0
        self.edit_count = 0
        self._tools: dict[str, Callable[..., dict[str, Any]]] = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "search_code": self._search_code,
            "edit_file": self._edit_file,
            "run_tests": self._run_tests,
            "get_git_diff": self._get_git_diff,
            "repo_overview": self._repo_overview,
        }

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    def call(self, tool_name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        if not isinstance(args, dict):
            decision = FirewallDecision(
                allowed=False,
                risk=RiskLevel.DANGEROUS,
                reason=f"tool args must be an object, got {type(args).__name__}",
            )
            result = tool_result(
                tool=tool_name,
                ok=False,
                error=decision.reason,
            )
            self._audit(tool_name, {}, decision, result)
            return result

        tool = self._tools.get(tool_name)
        if tool is None:
            decision = FirewallDecision(
                allowed=False,
                risk=RiskLevel.DANGEROUS,
                reason=f"unknown tool: {tool_name}. Available tools: {', '.join(self.tool_names)}",
            )
            result = tool_result(
                tool=tool_name,
                ok=False,
                error=decision.reason,
            )
            self._audit(tool_name, args, decision, result)
            return result

        try:
            decision = self.firewall.check(tool_name, args)
            if decision.approval_required and decision.approved:
                self.approval_count += 1

            if not decision.allowed:
                self.blocked_call_count += 1
                result = tool_result(
                    tool=tool_name,
                    ok=False,
                    error=decision.reason,
                    meta={
                        "risk": decision.risk.value,
                        "approval_required": decision.approval_required,
                        "approved": decision.approved,
                        **decision.meta,
                    },
                )
                self._audit(tool_name, args, decision, result)
                return result

            result = tool(**args)
            self.call_count += 1
            if tool_name == "edit_file" and result.get("ok"):
                self.edit_count += 1
            self._audit(tool_name, args, decision, result)
            return result
        except TypeError as e:
            decision = FirewallDecision(
                allowed=False,
                risk=RiskLevel.DANGEROUS,
                reason=f"invalid tool arguments: {e}",
            )
            result = tool_result(tool=tool_name, ok=False, error=decision.reason)
            self._audit(tool_name, args, decision, result)
            return result
        except Exception as e:
            decision = FirewallDecision(
                allowed=False,
                risk=RiskLevel.DANGEROUS,
                reason=str(e),
            )
            result = tool_result(tool=tool_name, ok=False, error=str(e))
            self._audit(tool_name, args, decision, result)
            return result

    def _list_files(
        self,
        path: str = ".",
        max_depth: int = 3,
        max_entries: int = 200,
    ) -> dict[str, Any]:
        return list_files(self.repo_root, path, max_depth=max_depth, max_entries=max_entries)

    def _repo_overview(
        self,
        max_depth: int = 3,
        max_files: int = 120,
        max_chars_per_file: int = 2500,
    ) -> dict[str, Any]:
        return repo_overview(
            self.repo_root,
            max_depth=max_depth,
            max_files=max_files,
            max_chars_per_file=max_chars_per_file,
        )

    def _read_file(
        self,
        file_path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        return read_file(self.repo_root, file_path, start_line=start_line, end_line=end_line)

    def _search_code(
        self,
        keyword: str,
        path: str = ".",
        context_lines: int = 2,
    ) -> dict[str, Any]:
        return search_code(
            self.repo_root,
            keyword,
            path=path,
            context_lines=context_lines,
        )

    def _edit_file(self, file_path: str, old_text: str, new_text: str) -> dict[str, Any]:
        return edit_file(self.repo_root, file_path, old_text=old_text, new_text=new_text)

    def _run_tests(self, command: str | None = None, timeout: int | None = None) -> dict[str, Any]:
        if command is not None and command != self.test_command:
            return tool_result(
                tool="run_tests",
                ok=False,
                error="run_tests only allows the configured test command for this task",
                meta={"configured_command": self.test_command, "requested_command": command},
            )

        effective_timeout = timeout or self.test_timeout
        return run_tests(self.repo_root, self.test_command, timeout=effective_timeout)

    def _get_git_diff(self) -> dict[str, Any]:
        return get_git_diff(self.project_root)

    def metrics(self) -> dict[str, Any]:
        return {
            "tool_calls": self.call_count,
            "edit_count": self.edit_count,
            "blocked_tool_calls": self.blocked_call_count,
            "approval_count": self.approval_count,
        }

    def _audit(
        self,
        tool_name: str,
        args: dict[str, Any],
        decision: FirewallDecision,
        result: dict[str, Any],
    ) -> None:
        self.audit_logger.write(
            task_id=self.task_id,
            tool=tool_name,
            args=args,
            decision=decision,
            result=result,
        )
