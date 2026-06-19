from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class RiskLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DANGEROUS = "dangerous"


@dataclass(frozen=True)
class ToolPolicy:
    risk: RiskLevel
    approval_required: bool = False
    path_required: bool = False
    allowed_extensions: tuple[str, ...] = ()
    allowed_commands: tuple[str, ...] = ()
    timeout: int | None = None


@dataclass
class FirewallDecision:
    allowed: bool
    risk: RiskLevel
    approval_required: bool = False
    approved: bool = False
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


DEFAULT_POLICY: dict[str, ToolPolicy] = {
    "list_files": ToolPolicy(RiskLevel.READ, path_required=True),
    "repo_overview": ToolPolicy(RiskLevel.READ),
    "read_file": ToolPolicy(RiskLevel.READ, path_required=True),
    "search_code": ToolPolicy(RiskLevel.READ, path_required=True),
    "get_git_diff": ToolPolicy(RiskLevel.READ),
    "edit_file": ToolPolicy(
        RiskLevel.WRITE,
        approval_required=True,
        path_required=True,
        allowed_extensions=(".py", ".md", ".txt", ".json", ".yaml", ".yml"),
    ),
    "run_tests": ToolPolicy(
        RiskLevel.EXECUTE,
        allowed_commands=("pytest", "python -m pytest", "pytest tests", "pytest -q"),
        timeout=30,
    ),
}


DANGEROUS_COMMAND_PATTERNS = (
    r"\brm\b",
    r"\bdel\b",
    r"\brmdir\b",
    r"\bcurl\b",
    r"\bwget\b",
    r"\bscp\b",
    r"\bssh\b",
    r"\bpip\s+install\b",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bformat\b",
    r"\bshutdown\b",
)


class ToolFirewall:
    """Policy gate for LLM-selected tool calls.

    This is the project's guardrail layer: the agent may propose an action, but
    this firewall decides whether the tool can run inside the repository boundary.
    """

    def __init__(
        self,
        repo_root: str | Path,
        *,
        policy: dict[str, ToolPolicy] | None = None,
        configured_test_command: str = "pytest",
        approval_mode: str = "auto",
    ) -> None:
        if approval_mode not in {"auto", "prompt", "deny"}:
            raise ValueError("approval_mode must be one of: auto, prompt, deny")

        self.repo_root = Path(repo_root).resolve()
        self.policy = dict(policy or DEFAULT_POLICY)
        self.configured_test_command = configured_test_command
        self.approval_mode = approval_mode

    def check(self, tool_name: str, args: dict[str, Any]) -> FirewallDecision:
        policy = self.policy.get(tool_name)
        if policy is None:
            return FirewallDecision(
                allowed=False,
                risk=RiskLevel.DANGEROUS,
                reason=f"unknown tool: {tool_name}",
            )

        if tool_name == "run_tests":
            command = str(args.get("command") or self.configured_test_command)
            command_decision = self._check_command(command, policy)
            if not command_decision.allowed:
                return command_decision
            timeout_decision = self._check_timeout(args.get("timeout"), policy)
            if not timeout_decision.allowed:
                return timeout_decision

        if policy.path_required:
            path_arg = _path_argument_for(tool_name, args)
            if not path_arg:
                return FirewallDecision(
                    allowed=False,
                    risk=policy.risk,
                    reason=f"{tool_name} requires a repository-relative path",
                )

            path_decision = self._check_path(path_arg, policy)
            if not path_decision.allowed:
                return path_decision

        approved = True
        if policy.approval_required:
            approved = self._approve(tool_name, policy, args)
            if not approved:
                return FirewallDecision(
                    allowed=False,
                    risk=policy.risk,
                    approval_required=True,
                    approved=False,
                    reason="tool call requires approval and was not approved",
                )

        return FirewallDecision(
            allowed=True,
            risk=policy.risk,
            approval_required=policy.approval_required,
            approved=approved,
            reason="allowed by policy",
        )

    def _check_path(self, raw_path: str, policy: ToolPolicy) -> FirewallDecision:
        try:
            target = (self.repo_root / raw_path).resolve()
            target.relative_to(self.repo_root)
        except ValueError:
            return FirewallDecision(
                allowed=False,
                risk=policy.risk,
                reason=f"path escapes repo root: {raw_path}",
            )

        if _looks_sensitive(raw_path):
            return FirewallDecision(
                allowed=False,
                risk=RiskLevel.DANGEROUS,
                reason=f"sensitive path blocked: {raw_path}",
            )

        if policy.allowed_extensions and target.suffix not in policy.allowed_extensions:
            return FirewallDecision(
                allowed=False,
                risk=policy.risk,
                reason=(
                    f"file extension {target.suffix or '<none>'} is not allowed for this tool"
                ),
            )

        return FirewallDecision(
            allowed=True,
            risk=policy.risk,
            reason="path is inside repo root",
            meta={"resolved_path": str(target)},
        )

    def _check_command(self, command: str, policy: ToolPolicy) -> FirewallDecision:
        normalized = " ".join(command.strip().split())
        if not normalized:
            return FirewallDecision(
                allowed=False,
                risk=RiskLevel.EXECUTE,
                reason="command is empty",
            )

        for pattern in DANGEROUS_COMMAND_PATTERNS:
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                return FirewallDecision(
                    allowed=False,
                    risk=RiskLevel.DANGEROUS,
                    reason=f"dangerous command blocked: {normalized}",
                )

        allowed_commands = set(policy.allowed_commands)
        allowed_commands.add(self.configured_test_command)
        if normalized not in allowed_commands:
            return FirewallDecision(
                allowed=False,
                risk=RiskLevel.EXECUTE,
                reason=f"command is not on the allowlist: {normalized}",
                meta={"allowed_commands": sorted(allowed_commands)},
            )

        return FirewallDecision(
            allowed=True,
            risk=RiskLevel.EXECUTE,
            reason="command is on the allowlist",
            meta={"command": normalized, "timeout": policy.timeout},
        )

    def _check_timeout(self, raw_timeout: Any, policy: ToolPolicy) -> FirewallDecision:
        if raw_timeout is None or policy.timeout is None:
            return FirewallDecision(
                allowed=True,
                risk=RiskLevel.EXECUTE,
                reason="timeout accepted",
            )

        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError):
            return FirewallDecision(
                allowed=False,
                risk=RiskLevel.EXECUTE,
                reason=f"timeout must be an integer, got {raw_timeout!r}",
            )

        if timeout <= 0 or timeout > policy.timeout:
            return FirewallDecision(
                allowed=False,
                risk=RiskLevel.EXECUTE,
                reason=f"timeout must be between 1 and {policy.timeout} seconds",
            )

        return FirewallDecision(
            allowed=True,
            risk=RiskLevel.EXECUTE,
            reason="timeout accepted",
            meta={"timeout": timeout},
        )

    def _approve(self, tool_name: str, policy: ToolPolicy, args: dict[str, Any]) -> bool:
        if self.approval_mode == "auto":
            return True
        if self.approval_mode == "deny":
            return False

        file_path = args.get("file_path") or args.get("path") or "<none>"
        print()
        print("Tool call requires approval:")
        print(f"tool: {tool_name}")
        print(f"risk: {policy.risk.value}")
        print("reason: modifies repository state")
        print(f"file: {file_path}")
        answer = input("Approve? [y/N] ").strip().lower()
        return answer in {"y", "yes"}


class AuditLogger:
    def __init__(self, log_file: str | Path) -> None:
        self.log_file = Path(log_file)

    def write(
        self,
        *,
        task_id: str | None,
        tool: str,
        args: dict[str, Any],
        decision: FirewallDecision,
        result: dict[str, Any] | None,
    ) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "task_id": task_id,
            "tool": tool,
            "risk": decision.risk.value,
            "args_summary": _summarize_args(args),
            "allowed": decision.allowed,
            "approval_required": decision.approval_required,
            "approved": decision.approved,
            "reason": decision.reason,
            "result_ok": None if result is None else result.get("ok"),
        }
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _path_argument_for(tool_name: str, args: dict[str, Any]) -> str | None:
    if tool_name in {"read_file", "edit_file"}:
        return args.get("file_path")
    if tool_name in {"list_files", "search_code"}:
        return args.get("path", ".")
    return args.get("path") or args.get("file_path")


def _looks_sensitive(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    sensitive_parts = (".ssh/", "/.ssh/", "id_rsa", "passwd", ".env")
    return any(part in normalized for part in sensitive_parts)


def _summarize_args(args: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"old_text", "new_text", "content"} and isinstance(value, str):
            summary[key] = f"<{len(value)} chars>"
        else:
            summary[key] = value
    return summary
