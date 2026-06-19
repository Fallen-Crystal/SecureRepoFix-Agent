# tools/basic_tools.py

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
}

IGNORED_FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".zip",
    ".tar",
    ".gz",
}

OVERVIEW_FILES = (
    "README.md",
    "README.rst",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "tox.ini",
    "pytest.ini",
)


def tool_result(
    *,
    tool: str,
    ok: bool,
    content: str = "",
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "tool": tool,
        "content": content,
        "error": error,
        "meta": meta or {},
    }


def _resolve_repo_root(repo_root: str | Path) -> Path:
    root = Path(repo_root).resolve()

    if not root.exists():
        raise FileNotFoundError(f"repo_root not found: {root}")

    if not root.is_dir():
        raise NotADirectoryError(f"repo_root is not a directory: {root}")

    return root


def _safe_path(repo_root: str | Path, relative_path: str | Path) -> Path:
    root = _resolve_repo_root(repo_root)
    target = (root / relative_path).resolve()

    try:
        target.relative_to(root)
    except ValueError:
        raise PermissionError(f"path escapes repo root: {relative_path}")

    return target


def _truncate(text: str, max_chars: int = 12000) -> str:
    if len(text) <= max_chars:
        return text

    return text[:max_chars] + f"\n\n[truncated: original length {len(text)} chars]"


def list_files(
    repo_root: str | Path,
    path: str | Path = ".",
    max_depth: int = 3,
    max_entries: int = 200,
) -> dict[str, Any]:
    tool = "list_files"

    try:
        root = _resolve_repo_root(repo_root)
        start = _safe_path(root, path)

        if not start.exists():
            return tool_result(tool=tool, ok=False, error=f"path not found: {path}")

        if not start.is_dir():
            return tool_result(tool=tool, ok=False, error=f"path is not a directory: {path}")

        lines: list[str] = []
        count = 0

        def walk(current: Path, depth: int) -> None:
            nonlocal count

            if depth > max_depth or count >= max_entries:
                return

            entries = sorted(
                current.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower())
            )

            for entry in entries:
                if count >= max_entries:
                    break

                if entry.name in IGNORED_DIRS:
                    continue

                rel = entry.relative_to(root)
                indent = "  " * depth

                if entry.is_dir():
                    lines.append(f"{indent}{rel}/")
                    count += 1
                    walk(entry, depth + 1)
                else:
                    if entry.suffix.lower() in IGNORED_FILE_SUFFIXES:
                        continue

                    lines.append(f"{indent}{rel}")
                    count += 1

        walk(start, 0)

        return tool_result(
            tool=tool,
            ok=True,
            content="\n".join(lines),
            meta={
                "count": count,
                "max_depth": max_depth,
                "truncated": count >= max_entries,
            },
        )

    except Exception as e:
        return tool_result(tool=tool, ok=False, error=str(e))


def repo_overview(
    repo_root: str | Path,
    max_depth: int = 3,
    max_files: int = 120,
    max_chars_per_file: int = 2500,
) -> dict[str, Any]:
    tool = "repo_overview"

    try:
        root = _resolve_repo_root(repo_root)
        files: list[Path] = []

        for file in root.rglob("*"):
            if not file.is_file():
                continue
            rel = file.relative_to(root)
            if any(part in IGNORED_DIRS for part in rel.parts):
                continue
            if file.suffix.lower() in IGNORED_FILE_SUFFIXES:
                continue
            if len(rel.parts) > max_depth:
                continue
            files.append(rel)

        files = sorted(files, key=lambda p: (len(p.parts), str(p).lower()))[:max_files]
        sections = [
            "[files]",
            "\n".join(str(path).replace("\\", "/") for path in files) or "no files found",
        ]

        for name in OVERVIEW_FILES:
            path = root / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            sections.append(f"\n[{name}]")
            sections.append(_truncate(text.strip(), max_chars=max_chars_per_file))

        test_files = [
            path for path in files
            if path.name.startswith("test") or path.name.startswith("tests")
        ][:10]
        if test_files:
            sections.append("\n[likely test files]")
            sections.append("\n".join(str(path).replace("\\", "/") for path in test_files))

        return tool_result(
            tool=tool,
            ok=True,
            content="\n".join(sections),
            meta={
                "file_count": len(files),
                "max_depth": max_depth,
                "truncated": len(files) >= max_files,
            },
        )

    except Exception as e:
        return tool_result(tool=tool, ok=False, error=str(e))


def read_file(
    repo_root: str | Path,
    file_path: str | Path,
    start_line: int = 1,
    end_line: int | None = None,
    max_chars: int = 12000,
) -> dict[str, Any]:
    tool = "read_file"

    try:
        target = _safe_path(repo_root, file_path)

        if not target.exists():
            return tool_result(tool=tool, ok=False, error=f"file not found: {file_path}")

        if not target.is_file():
            return tool_result(tool=tool, ok=False, error=f"not a file: {file_path}")

        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()

        if start_line < 1:
            start_line = 1

        if end_line is None or end_line > len(lines):
            end_line = len(lines)

        selected = lines[start_line - 1:end_line]

        numbered = [
            f"{line_no:>4} | {line}"
            for line_no, line in enumerate(selected, start=start_line)
        ]

        content = _truncate("\n".join(numbered), max_chars=max_chars)

        return tool_result(
            tool=tool,
            ok=True,
            content=content,
            meta={
                "file_path": str(file_path),
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": len(lines),
            },
        )

    except UnicodeDecodeError:
        return tool_result(tool=tool, ok=False, error=f"cannot decode file as utf-8: {file_path}")
    except Exception as e:
        return tool_result(tool=tool, ok=False, error=str(e))


def search_code(
    repo_root: str | Path,
    keyword: str,
    path: str | Path = ".",
    context_lines: int = 2,
    max_results: int = 30,
    max_chars: int = 12000,
) -> dict[str, Any]:
    tool = "search_code"

    try:
        if not keyword.strip():
            return tool_result(tool=tool, ok=False, error="keyword is empty")

        root = _resolve_repo_root(repo_root)
        start = _safe_path(root, path)

        if not start.exists():
            return tool_result(tool=tool, ok=False, error=f"path not found: {path}")

        results: list[str] = []
        match_count = 0

        files = [start] if start.is_file() else start.rglob("*")

        for file in files:
            if match_count >= max_results:
                break

            if not file.is_file():
                continue

            if any(part in IGNORED_DIRS for part in file.parts):
                continue

            if file.suffix.lower() in IGNORED_FILE_SUFFIXES:
                continue

            try:
                text = file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue

            lines = text.splitlines()

            for i, line in enumerate(lines):
                if keyword in line:
                    rel = file.relative_to(root)
                    begin = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)

                    results.append(f"\n--- {rel}:{i + 1} ---")

                    for j in range(begin, end):
                        marker = ">" if j == i else " "
                        results.append(f"{marker} {j + 1:>4} | {lines[j]}")

                    match_count += 1

                    if match_count >= max_results:
                        break

        content = "\n".join(results)

        if not content:
            content = f"no matches found for keyword: {keyword}"

        return tool_result(
            tool=tool,
            ok=True,
            content=_truncate(content, max_chars=max_chars),
            meta={
                "keyword": keyword,
                "matches": match_count,
                "truncated": match_count >= max_results,
            },
        )

    except Exception as e:
        return tool_result(tool=tool, ok=False, error=str(e))


def edit_file(
    repo_root: str | Path,
    file_path: str | Path,
    old_text: str,
    new_text: str,
) -> dict[str, Any]:
    tool = "edit_file"

    try:
        if not old_text:
            return tool_result(tool=tool, ok=False, error="old_text cannot be empty")

        target = _safe_path(repo_root, file_path)

        if not target.exists():
            return tool_result(tool=tool, ok=False, error=f"file not found: {file_path}")

        if not target.is_file():
            return tool_result(tool=tool, ok=False, error=f"not a file: {file_path}")

        text = target.read_text(encoding="utf-8")
        count = text.count(old_text)

        if count == 0:
            return tool_result(
                tool=tool,
                ok=False,
                error="old_text not found in file",
                meta={"file_path": str(file_path)},
            )

        if count > 1:
            return tool_result(
                tool=tool,
                ok=False,
                error=f"old_text appears {count} times; please provide a more specific snippet",
                meta={"file_path": str(file_path), "matches": count},
            )

        updated = text.replace(old_text, new_text, 1)
        target.write_text(updated, encoding="utf-8")

        return tool_result(
            tool=tool,
            ok=True,
            content=f"edited file: {file_path}",
            meta={
                "file_path": str(file_path),
                "old_chars": len(old_text),
                "new_chars": len(new_text),
            },
        )

    except UnicodeDecodeError:
        return tool_result(tool=tool, ok=False, error=f"cannot decode file as utf-8: {file_path}")
    except Exception as e:
        return tool_result(tool=tool, ok=False, error=str(e))


def run_tests(
    repo_root: str | Path,
    command: str = "pytest",
    timeout: int = 30,
    max_chars: int = 16000,
) -> dict[str, Any]:
    tool = "run_tests"

    try:
        root = _resolve_repo_root(repo_root)

        completed = subprocess.run(
            command,
            cwd=root,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

        output: list[str] = []
        output.append(f"$ {command}")
        output.append(f"exit_code: {completed.returncode}")

        if completed.stdout:
            output.append("\n[stdout]")
            output.append(completed.stdout)

        if completed.stderr:
            output.append("\n[stderr]")
            output.append(completed.stderr)

        return tool_result(
            tool=tool,
            ok=True,
            content=_truncate("\n".join(output), max_chars=max_chars),
            meta={
                "command": command,
                "exit_code": completed.returncode,
                "passed": completed.returncode == 0,
            },
        )

    except subprocess.TimeoutExpired as e:
        stdout = e.stdout or ""
        stderr = e.stderr or ""

        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="ignore")

        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="ignore")

        return tool_result(
            tool=tool,
            ok=False,
            error=f"test command timed out after {timeout}s",
            content=_truncate(stdout + "\n" + stderr, max_chars=max_chars),
            meta={"command": command, "timeout": timeout},
        )

    except Exception as e:
        return tool_result(tool=tool, ok=False, error=str(e))


def get_git_diff(
    repo_root: str | Path,
    max_chars: int = 20000,
) -> dict[str, Any]:
    tool = "get_git_diff"

    try:
        root = _resolve_repo_root(repo_root)

        completed = subprocess.run(
            ["git", "diff"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=10,
        )

        if completed.returncode != 0:
            return tool_result(
                tool=tool,
                ok=False,
                error=completed.stderr.strip() or "git diff failed",
                meta={"exit_code": completed.returncode},
            )

        diff = completed.stdout.strip()

        if not diff:
            diff = "no git diff"

        return tool_result(
            tool=tool,
            ok=True,
            content=_truncate(diff, max_chars=max_chars),
            meta={"has_diff": diff != "no git diff"},
        )

    except Exception as e:
        return tool_result(tool=tool, ok=False, error=str(e))
