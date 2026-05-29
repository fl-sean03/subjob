"""Local backend — run workers as in-process subprocesses, no scheduler.

For testing and single-node use. Spawns `python -m subjob.worker` against the
pool and tracks the Popen handles so liveness can be polled. partition/qos/gpus
are no-ops here.
"""

from __future__ import annotations

import subprocess
import sys

from subjob.backends.base import WorkerHandle, WorkerStatus


class LocalBackend:
    name = "local"

    def __init__(self, python_executable: str | None = None):
        self.python_executable = python_executable or sys.executable
        self._procs: list[subprocess.Popen] = []

    def submit_worker(
        self,
        *,
        pool_dir: str,
        cores: int,
        gpus: int = 0,
        walltime_seconds: int = 86400,
        partition: str | None = None,
        qos: str | None = None,
        idle_timeout_seconds: float | None = None,
        extra_sbatch_args: list[str] | None = None,
    ) -> WorkerHandle:
        proc = subprocess.Popen(
            [
                self.python_executable,
                "-m",
                "subjob.worker",
                "--pool",
                pool_dir,
                "--cores",
                str(cores),
                "--idle-timeout",
                str(idle_timeout_seconds or 10),
                "--log-level",
                "WARNING",
            ]
        )
        self._procs.append(proc)
        return WorkerHandle(backend="local", job_id=str(proc.pid), metadata={"pool_dir": pool_dir})

    def count_workers(self, pool_dir: str) -> int:
        """Number of tracked worker subprocesses still alive."""
        return sum(1 for p in self._procs if p.poll() is None)

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        for p in self._procs:
            if str(p.pid) == handle.job_id:
                if p.poll() is None:
                    return WorkerStatus(state="running")
                return WorkerStatus(state="completed", detail=f"returncode={p.returncode}")
        return WorkerStatus(state="unknown", detail="pid not tracked by this backend")

    def cancel(self, handle: WorkerHandle) -> None:
        for p in self._procs:
            if str(p.pid) == handle.job_id and p.poll() is None:
                p.terminate()
                return
