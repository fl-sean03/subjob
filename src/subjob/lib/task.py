"""Task — the canonical work unit in a subjob pool.

YAML schema mirrors docs/ARCHITECTURE.md § "Task spec". Phase 0 parses every
field (so YAMLs written for later phases survive a round-trip) but only acts
on the fields the worker uses today: id, command, env, resources, priority,
walltime.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from subjob.lib import yaml_lite

ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+$")
ID_MAX_LEN = 200


@dataclass
class Resources:
    cores: int = 1
    gpus: int = 0
    memory_gb: int = 0
    walltime_seconds: int = 3600

    def to_dict(self) -> dict[str, int]:
        return {
            "cores": self.cores,
            "gpus": self.gpus,
            "memory_gb": self.memory_gb,
            "walltime_seconds": self.walltime_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Resources:
        d = d or {}
        return cls(
            cores=int(d.get("cores", 1)),
            gpus=int(d.get("gpus", 0)),
            memory_gb=int(d.get("memory_gb", 0)),
            walltime_seconds=int(d.get("walltime_seconds", 3600)),
        )


@dataclass
class Task:
    id: str
    command: str
    priority: int = 0
    state: str = "pending"
    env: dict[str, str] = field(default_factory=dict)
    resources: Resources = field(default_factory=Resources)
    backend_hints: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    retry: dict[str, Any] = field(default_factory=dict)
    priors_apply: list[str] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self) -> None:
        validate_id(self.id)
        if not self.command or not self.command.strip():
            raise ValueError(f"task {self.id!r}: command must be non-empty")
        if not self.created_at:
            self.created_at = _iso_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "priority": self.priority,
            "state": self.state,
            "command": self.command,
            "env": dict(self.env),
            "resources": self.resources.to_dict(),
            "backend_hints": dict(self.backend_hints),
            "artifacts": dict(self.artifacts),
            "depends_on": list(self.depends_on),
            "retry": dict(self.retry),
            "priors_apply": list(self.priors_apply),
            "attempts": list(self.attempts),
            "created_at": self.created_at,
        }

    def to_yaml(self) -> str:
        return yaml_lite.dumps(self.to_dict())

    def write(self, path: Path) -> None:
        path.write_text(self.to_yaml())

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Task:
        env = d.get("env") or {}
        env = {str(k): str(v) for k, v in env.items()}
        return cls(
            id=d["id"],
            command=d["command"],
            priority=int(d.get("priority", 0)),
            state=d.get("state") or "pending",
            env=env,
            resources=Resources.from_dict(d.get("resources")),
            backend_hints=d.get("backend_hints") or {},
            artifacts=d.get("artifacts") or {},
            depends_on=list(d.get("depends_on") or []),
            retry=d.get("retry") or {},
            priors_apply=list(d.get("priors_apply") or []),
            attempts=list(d.get("attempts") or []),
            created_at=d.get("created_at") or "",
        )

    @classmethod
    def from_yaml(cls, text: str) -> Task:
        return cls.from_dict(yaml_lite.loads(text))

    @classmethod
    def read(cls, path: Path) -> Task:
        return cls.from_yaml(path.read_text())


def validate_id(task_id: str) -> None:
    """Reject task IDs that aren't safe filenames or are too long."""
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task id must be a non-empty string")
    if len(task_id) > ID_MAX_LEN:
        raise ValueError(f"task id too long ({len(task_id)} > {ID_MAX_LEN})")
    if not ID_PATTERN.match(task_id):
        raise ValueError(
            f"task id {task_id!r} must match {ID_PATTERN.pattern} "
            "(filesystem-safe characters only)"
        )


def _iso_now() -> str:
    # UTC, second-precision, RFC3339-ish — no external deps.
    t = time.gmtime()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)
