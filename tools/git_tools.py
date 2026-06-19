from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from tools.basic_tools import tool_result


def clone_repository(
    repo_url: str,
    destination_root: str | Path,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    tool = "clone_repository"

    try:
        if not _is_allowed_repo_url(repo_url):
            return tool_result(
                tool=tool,
                ok=False,
                error="repo_url must be an https GitHub URL or git@github.com SSH URL",
            )

        root = Path(destination_root).resolve()
        root.mkdir(parents=True, exist_ok=True)

        repo_name = name or _repo_name_from_url(repo_url)
        if not repo_name:
            return tool_result(tool=tool, ok=False, error="could not infer repository name")

        destination = (root / repo_name).resolve()
        destination.relative_to(root)

        if destination.exists():
            return tool_result(
                tool=tool,
                ok=False,
                error=f"destination already exists: {destination}",
                meta={"repo_path": str(destination)},
            )

        completed = subprocess.run(
            ["git", "clone", repo_url, str(destination)],
            text=True,
            capture_output=True,
            timeout=120,
        )

        if completed.returncode != 0:
            return tool_result(
                tool=tool,
                ok=False,
                content=completed.stdout,
                error=completed.stderr.strip() or "git clone failed",
                meta={"exit_code": completed.returncode},
            )

        return tool_result(
            tool=tool,
            ok=True,
            content=completed.stdout + completed.stderr,
            meta={"repo_path": str(destination), "repo_url": repo_url},
        )

    except subprocess.TimeoutExpired:
        return tool_result(tool=tool, ok=False, error="git clone timed out")
    except Exception as e:
        return tool_result(tool=tool, ok=False, error=str(e))


def _is_allowed_repo_url(repo_url: str) -> bool:
    return bool(
        re.match(r"^https://github\.com/[^/\s]+/[^/\s]+(?:\.git)?$", repo_url)
        or re.match(r"^git@github\.com:[^/\s]+/[^/\s]+(?:\.git)?$", repo_url)
    )


def _repo_name_from_url(repo_url: str) -> str:
    name = repo_url.rstrip("/").split("/")[-1]
    if ":" in name:
        name = name.split(":")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)
