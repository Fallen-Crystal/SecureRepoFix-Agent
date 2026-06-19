# SecureRepoFix-Agent

SecureRepoFix-Agent is a controlled LLM Agent engineering system for autonomous
repository repair. It accepts a bug issue, lets an agent inspect and edit a real
repository only through approved tools, validates the repair with tests, records
audit logs, and saves benchmark trajectories.

The project is intentionally more than an "AI coding assistant": it keeps a
clear Agent-Computer Interface, a Tool Firewall, a MemoryAdapter boundary for a
future TaskMemory-Agent project, and benchmark metrics for success rate, tool
use, blocked actions, approvals, and failure types.

See [docs/secure_repofix_design.md](docs/secure_repofix_design.md) for the
full design map and the explicit research/security borrowing list.

## Current Architecture

The project is intentionally split into four layers:

- `agent/`: ReAct loop, decision models, memory adapter, tool firewall, and tool registry.
- `tools/`: safe codebase operations such as file search, file editing, tests, and GitHub import.
- `benchmark/`: task loading, task selection, runner CLI, results, and trajectories.
- `sandbox_projects/`: local repositories used for benchmark and development.
- `logs/`: local audit and placeholder memory JSONL files, ignored by Git.

This split keeps the model, tool execution, and benchmark orchestration separate.
The motivation is that each layer evolves independently: the model can move from
a scripted offline model to a real LLM without rewriting the tools or runner.

## Run Benchmark Task

```powershell
python benchmark\run_one_task.py 01 --mode agent --model-provider scripted
```

The scripted provider is offline and deterministic. It is used to verify the
ReAct loop without depending on an API key.

Benchmark tasks can be selected by 1-based number or task id:

```powershell
python benchmark\run_one_task.py 03 --mode patch
python benchmark\run_one_task.py multi_003 --mode agent --model-provider llm
```

`calc_001` is only a smoke test for the execution loop. The project is meant to
be demonstrated with multi-file and real repository tasks, not with calculator
arithmetic alone.

To run against a real GitHub repository, provide `--repo-url` and enable
`--discover-issue` so the runner first executes tests, infers the failing
behavior, and then repairs it:

```powershell
python benchmark\run_one_task.py `
  --repo-url https://github.com/owner/repo.git `
  --repo-name my_repo_run `
  --discover-issue `
  --test-command pytest `
  --mode agent `
  --model-provider llm
```

Per-run Markdown reports are written to `benchmark\reports\`, trajectories to
`benchmark\trajectories\`, and the latest aggregated benchmark rows to
`benchmark\results.jsonl`.

## Sandbox Benchmark Suite

`sandbox_projects/` contains small, local repositories that simulate common bug
patterns before running on real GitHub repositories.

| Task | Sandbox repo | Purpose |
| --- | --- | --- |
| `calc_001` | `calculator` | basic single-file arithmetic bug |
| `string_002` | `string_utils` | single-file logic and order preservation |
| `multi_003` | `multi_file_service` | cross-file bug localization |
| `exception_004` | `exception_handling` | boundary cases and exception handling |
| `cli_005` | `cli_csv` | command-line behavior verified by tests |
| `config_006` | `config_loader` | config/data-file reading, not just pure functions |
| `issue_007` | `issue_only_discount` | clear issue description with no expected-file hint |

Each sandbox repo includes its own pytest tests. The benchmark runner resets
buggy files from `benchmark/tasks.json` before each run, so tasks remain
repeatable even after a previous run fixed the files.

For local offline validation, run the suite against `benchmark\tasks.json`.
For real evaluation, point `--suite` at `benchmark\real_tasks.example.json`
or your own GitHub-backed suite file.

## Run With DeepSeek API

Set a DeepSeek API key first:

```powershell
$env:DEEPSEEK_API_KEY="your_api_key"
python benchmark\run_one_task.py 01 --mode agent --model-provider llm --model deepseek-v4-flash
```

Optional environment variables:

- `REPOFIX_MODEL`: default model name, currently `deepseek-v4-flash`.
- `DEEPSEEK_BASE_URL`: custom DeepSeek-compatible endpoint, default `https://api.deepseek.com`.

For the current test stage, `deepseek-v4-flash` is the recommended default:
it is cheap enough for repeated benchmark loops while still using the same
OpenAI-compatible chat-completions API shape as larger DeepSeek models. The
agent disables DeepSeek thinking mode by default because this workflow needs
short structured JSON actions rather than long reasoning output.

## Use TaskMemory-Agent

The runner can use the sibling `TaskMemory-Agent` project as the memory backend:

```powershell
python benchmark\run_one_task.py 01 `
  --mode agent `
  --model-provider llm `
  --memory-provider taskmemory
```

By default, RepoFix looks for TaskMemory at `..\TaskMemory-Agent` and stores its
JSONL memory under `logs\taskmemory.memories.jsonl` when
`--memory-provider taskmemory` is selected. Override these paths when needed:

```powershell
python benchmark\run_one_task.py 01 `
  --mode agent `
  --model-provider llm `
  --memory-provider taskmemory `
  --taskmemory-path C:\path\to\TaskMemory-Agent `
  --memory-file logs\taskmemory.memories.jsonl
```

Use this mode for experiments that need cross-run reflections, reusable repair
skills, and compressed long-horizon task state. Keep the default `jsonl` provider
for cheap baseline comparisons.

## Run A Task Suite

Use a suite file to run several local or GitHub bug repositories in one command:

```powershell
python benchmark\run_task_suite.py `
  --suite benchmark\real_tasks.example.json `
  --model-provider llm `
  --memory-provider taskmemory `
  --unique-repo-name
```

Each suite item can define `repo_url` or `repo_path`, `discover_issue`,
`seed_patches`, `test_command`, `setup_commands`, `test_timeout`, and
`max_steps`. Use `--unique-repo-name` for GitHub tasks when you want repeatable
runs without colliding with an earlier clone under `sandbox_projects\imported`.

The runner writes one Markdown report per task under `benchmark\reports\` and
the suite runner writes a summary report:

```powershell
python benchmark\run_task_suite.py `
  --suite benchmark\medium_tasks.example.json `
  --model-provider llm `
  --memory-provider taskmemory `
  --unique-repo-name `
  --report benchmark\reports\medium_suite_latest.md
```

`benchmark\medium_tasks.example.json` is the next-stage evaluation suite. It
keeps the `python-slugify` task enabled and injects the seed bug locally after
clone, then includes disabled templates for larger forks such as `humanize`,
`click`, and `packaging`. Flip `"enabled"` to `true` only after the fork exists
and a suitable `seed_patches` entry has been added.

## Current Benchmark Status

The local offline suite currently passes all 7 tasks in `benchmark\tasks.json`:
single-file logic, multi-file calls, exception handling, CLI behavior, config
loading, and issue recovery.

Representative results are stored under:

- [local suite report](benchmark/reports/local_suite_latest.md)
- [real GitHub smoke run report](benchmark/reports/2026-06-19T212042_github_cart_demo_20260619b.md)
- [real GitHub trajectory](benchmark/trajectories/github_cart_demo_20260619b-20260619-212042.json)

This means the project now has both a reproducible offline benchmark and at
least one real GitHub repair run to show in interviews.

## Run Against A Local Repository

```powershell
python benchmark\run_one_task.py `
  --repo-path path\to\repo `
  --issue "Describe the bug here" `
  --test-command "pytest" `
  --mode agent `
  --model-provider llm
```

## Import A GitHub Repository

To let the agent discover the issue from failing tests instead of providing a
human-written bug report:

```powershell
python benchmark\run_one_task.py `
  --repo-url https://github.com/owner/repo.git `
  --discover-issue `
  --test-command "pytest" `
  --test-timeout 60 `
  --setup-command "python -m pip install -e ." `
  --mode agent `
  --model-provider llm `
  --max-steps 12
```

With `--discover-issue`, the runner first lists files and runs the configured
test command. If tests fail, DeepSeek summarizes the failure into a concise issue
and feeds that issue back into the repair agent along with the failing test
output.

You can still provide a manual issue when you want a targeted repair:

```powershell
python benchmark\run_one_task.py `
  --repo-url https://github.com/owner/repo.git `
  --issue "Describe the bug here" `
  --test-command "pytest" `
  --mode agent `
  --model-provider llm
```

Imported repositories are cloned under `sandbox_projects/imported/`, which is
ignored by Git to avoid accidentally committing external projects.

Setup commands are run before issue discovery and repair. They are suite- or
operator-provided, not model-proposed actions, and obviously destructive command
patterns such as `rm`, `git push`, and `git reset --hard` are blocked. Use setup
only for reproducible repository initialization such as editable installs.

Seed patches are also runner-provided rather than model-proposed. They make a
clean cloned repository fail in a controlled way, which lets the same real
repository be used repeatedly without manually pushing broken code to GitHub.

## Design Motivation

The current goal is not to fully clone SWE-agent, but to build a small,
explainable repair agent:

1. The model decides the next action.
2. The memory adapter provides relevant goals, failures, and reusable experience.
3. The tool firewall classifies risk and enforces policy before execution.
4. The tool registry executes only allowed operations.
5. The ReAct loop stores thought, action, observation, reflection, and final result.
6. The benchmark runner provides repeatable evaluation and audit metrics.

This gives the project a clear path from a toy calculator task to real GitHub
repositories.

## Security And Memory Hooks

All agent tool calls go through `ToolRegistry` and `ToolFirewall`.

- Read tools are restricted to paths inside `repo_root`.
- Write tools require policy approval and extension checks.
- Test execution is constrained to the configured command or pytest allowlist.
- Dangerous commands such as `rm`, `curl`, `ssh`, `pip install`, `git push`, and
  `git reset --hard` are blocked by default.
- Every tool decision is written to `logs/audit.jsonl`.

Project one does not implement the full long-term memory system. It defines
`MemoryAdapter` in `agent/memory.py` and ships a local `JsonlMemoryAdapter`
placeholder so the future TaskMemory-Agent can plug in through the same
`retrieve/write/update/summarize` interface.
