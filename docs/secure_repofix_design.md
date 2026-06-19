# SecureRepoFix-Agent Design

SecureRepoFix-Agent is the main repository-repair system. It is not positioned
as a generic code assistant; it is an LLM agent engineering system with a
controlled Agent-Computer Interface, a tool firewall, memory adapter hooks, and
benchmark traces.

## Main Flow

```text
issue description
  -> agent extracts task goal and constraints
  -> MemoryAdapter writes goal memory
  -> agent inspects repository through controlled tools
  -> ToolFirewall reviews every tool call
  -> tool executes inside repo-root sandbox
  -> audit log records decision and result
  -> pytest validates the repair
  -> failed tests write reflection memory
  -> successful tests write skill memory
  -> benchmark records success, tool use, approvals, blocks, and failure type
```

## Components

| Component | File | Responsibility |
| --- | --- | --- |
| Agent loop | `agent/loop.py` | ReAct loop, stop conditions, test feedback, memory touchpoints |
| Tool registry | `agent/tool_registry.py` | Single controlled ACI entrypoint for all tools |
| Tool firewall | `agent/firewall.py` | Risk classification, path sandbox, command allowlist, approval checks, audit logging |
| Memory adapter | `agent/memory.py` | Stable interface reserved for the future TaskMemory-Agent project |
| Benchmark runner | `benchmark/run_one_task.py` | Repeatable task execution and result JSONL metrics |

## Research And Security Borrowing Map

The technical inspirations are preserved explicitly instead of hidden in vague
implementation choices.

| Source | Borrowed idea | Project implementation |
| --- | --- | --- |
| SWE-agent, "Agent-Computer Interfaces Enable Automated Software Engineering" | Give the LLM a narrow, purpose-built interface for repository navigation, edits, and tests. | `ToolRegistry` exposes `list_files`, `read_file`, `search_code`, `edit_file`, `run_tests`, and `get_git_diff` as the only agent tools. |
| Reflexion, "Language Agents with Verbal Reinforcement Learning" | Convert failure feedback into language-level reflection for later attempts. | `ReActAgent` writes `reflection` memory events when tests fail. |
| MemGPT, "Towards LLMs as Operating Systems" | Keep long-running task state behind a memory-management interface instead of stuffing all history into the prompt. | `MemoryAdapter` defines `retrieve`, `write`, `update`, and `summarize`; `JsonlMemoryAdapter` is a local placeholder until TaskMemory-Agent is connected. |
| OWASP Top 10 for LLM Applications | Agentic systems must defend against prompt injection, sensitive data exposure, and excessive agency. | `ToolFirewall` enforces path sandboxing, sensitive-path blocks, shell command allowlists, write approvals, and JSONL audit logs. |

References:

- SWE-agent paper: https://arxiv.org/abs/2405.15793
- Reflexion paper: https://arxiv.org/abs/2303.11366
- MemGPT paper: https://arxiv.org/abs/2310.08560
- OWASP Top 10 for LLM Applications: https://owasp.org/www-project-top-10-for-large-language-model-applications/

## Tool Firewall Policy

Risk levels:

| Risk | Examples | Default policy |
| --- | --- | --- |
| `read` | `list_files`, `read_file`, `search_code`, `get_git_diff` | Allow inside repo root |
| `write` | `edit_file` | Repo-root path sandbox, extension allowlist, audit log, approval policy |
| `execute` | `run_tests` | Configured command or allowlisted pytest commands, timeout |
| `dangerous` | `rm`, `curl`, `ssh`, `pip install`, `git push`, `git reset --hard` | Block by default |

All path-bearing tool calls must resolve inside `repo_root`. Sensitive targets
such as `.ssh`, `id_rsa`, `.env`, and passwd-like files are blocked even when a
relative path would otherwise stay inside the repository.

## Memory Adapter Boundary

Project one does not implement the full memory system. It owns only the adapter
contract:

```python
class MemoryAdapter:
    def retrieve(self, query: str, scope: dict) -> dict: ...
    def write(self, event: dict) -> None: ...
    def update(self, memory_id: str, patch: dict) -> None: ...
    def summarize(self, task_id: str) -> dict: ...
```

The second project can replace `JsonlMemoryAdapter` with a network service,
database-backed memory, vector search, or episodic/skill memory store without
rewriting the main repair loop.

## Benchmark Record Fields

`benchmark/results.jsonl` includes the original repair outcome plus agent-system
metrics:

```json
{
  "task_id": "calc_001",
  "mode": "agent",
  "before_exit_code": 1,
  "after_exit_code": 0,
  "passed": true,
  "steps": 6,
  "tool_calls": 6,
  "edit_count": 1,
  "blocked_tool_calls": 0,
  "approval_count": 1,
  "memory_retrieval_count": 6,
  "constraint_violation": false,
  "failure_type": null
}
```

Tool-call audit records are written to `logs/audit.jsonl`, while local memory
placeholder records are written to `logs/memory.jsonl`.
