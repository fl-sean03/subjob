"""Backend abstraction.

A backend's responsibility is narrow: take a pool directory and resource
ask, launch a worker process against that pool, return a handle. The
worker itself is backend-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class WorkerHandle:
    backend: str
    job_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"backend": self.backend, "job_id": self.job_id, "metadata": self.metadata}


@dataclass
class WorkerStatus:
    state: str  # "queued", "running", "completed", "failed", "unknown"
    detail: str = ""


class Backend(Protocol):
    name: str

    def submit_worker(
        self,
        *,
        pool_dir: str,
        cores: int,
        gpus: int = 0,
        walltime_seconds: int = 86400,
        partition: str | None = None,
        qos: str | None = None,
        extra_sbatch_args: list[str] | None = None,
    ) -> WorkerHandle: ...

    def status(self, handle: WorkerHandle) -> WorkerStatus: ...

    def cancel(self, handle: WorkerHandle) -> None: ...
