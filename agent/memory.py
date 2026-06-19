from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


class MemoryAdapter(Protocol):
    """Integration point for a future TaskMemory-Agent service."""

    retrieval_count: int
    write_count: int

    def retrieve(self, query: str, scope: dict[str, Any]) -> dict[str, Any]:
        """Return relevant goals, constraints, failures, and reusable experience."""

    def write(self, event: dict[str, Any]) -> None:
        """Persist a goal, episodic event, reflection, or learned skill."""

    def update(self, memory_id: str, patch: dict[str, Any]) -> None:
        """Update an existing memory item in the backing memory project."""

    def summarize(self, task_id: str) -> dict[str, Any]:
        """Return a compact task summary for prompts and benchmark records."""


@dataclass
class NullMemoryAdapter:
    """No-op adapter used until the second memory project is attached."""

    retrieval_count: int = 0
    write_count: int = 0

    def retrieve(self, query: str, scope: dict[str, Any]) -> dict[str, Any]:
        self.retrieval_count += 1
        return {
            "query": query,
            "items": [],
            "adapter": "null",
            "note": "TaskMemory-Agent adapter not connected yet.",
        }

    def write(self, event: dict[str, Any]) -> None:
        self.write_count += 1

    def update(self, memory_id: str, patch: dict[str, Any]) -> None:
        self.write_count += 1

    def summarize(self, task_id: str) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "adapter": "null",
            "retrieval_count": self.retrieval_count,
            "write_count": self.write_count,
        }


@dataclass
class JsonlMemoryAdapter:
    """Small local adapter for development and tests.

    It is intentionally simple: the production memory system can later implement
    the same MemoryAdapter protocol without changing the agent loop.
    """

    path: Path
    retrieval_count: int = 0
    write_count: int = 0
    _events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._events.append(json.loads(line))

    def retrieve(self, query: str, scope: dict[str, Any]) -> dict[str, Any]:
        self.retrieval_count += 1
        task_id = scope.get("task_id")
        items = [
            event for event in self._events
            if event.get("task_id") == task_id or event.get("kind") in {"skill", "reflection"}
        ][-5:]
        return {
            "query": query,
            "items": items,
            "adapter": "jsonl",
        }

    def write(self, event: dict[str, Any]) -> None:
        self.write_count += 1
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            **event,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._events.append(payload)

    def update(self, memory_id: str, patch: dict[str, Any]) -> None:
        self.write(
            {
                "kind": "update",
                "memory_id": memory_id,
                "patch": patch,
            }
        )

    def summarize(self, task_id: str) -> dict[str, Any]:
        task_events = [event for event in self._events if event.get("task_id") == task_id]
        return {
            "task_id": task_id,
            "adapter": "jsonl",
            "events": len(task_events),
            "retrieval_count": self.retrieval_count,
            "write_count": self.write_count,
        }


def build_memory_adapter(
    provider: str,
    *,
    memory_file: Path,
    taskmemory_path: Path | None = None,
    taskmemory_audit_file: Path | None = None,
) -> MemoryAdapter:
    if provider == "null":
        return NullMemoryAdapter()

    if provider == "jsonl":
        return JsonlMemoryAdapter(memory_file)

    if provider == "taskmemory":
        if taskmemory_path is None:
            raise ValueError("taskmemory_path is required when provider is taskmemory")
        return _build_taskmemory_adapter(
            taskmemory_path=taskmemory_path,
            memory_file=memory_file,
            audit_file=taskmemory_audit_file,
        )

    raise ValueError(f"unknown memory provider: {provider}")


def _build_taskmemory_adapter(
    *,
    taskmemory_path: Path,
    memory_file: Path,
    audit_file: Path | None,
) -> MemoryAdapter:
    root = taskmemory_path.resolve()
    if not root.exists():
        raise FileNotFoundError(f"TaskMemory-Agent path not found: {root}")

    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)

    from adapters import RepoFixMemoryAdapter
    from memory.manager import TaskMemory
    from memory.store import JsonlMemoryStore

    store = JsonlMemoryStore(
        memory_file,
        audit_path=audit_file or memory_file.with_name("taskmemory.audit.jsonl"),
    )
    return RepoFixMemoryAdapter(TaskMemory(store))
