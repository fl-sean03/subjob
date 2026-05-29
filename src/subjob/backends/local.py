"""Local backend — run workers as in-process subprocesses, no scheduler.

For testing and single-node use. Spawns `python -m subjob.worker` against the
pool and tracks the Popen handles so liveness can be polled. partition/qos/gpus
are no-ops here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from subjob.backends.base import WorkerHandle, WorkerStatus

_REGISTRY_NAME = ".local_workers"


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
        cmd = [
            self.python_executable,
            "-m",
            "subjob.worker",
            "--pool",
            pool_dir,
            "--cores",
            str(cores),
            "--idle-timeout",
            str(idle_timeout_seconds or 10),
            "--walltime-seconds",
            str(walltime_seconds),
            "--log-level",
            "WARNING",
        ]
        if gpus > 0:
            cmd += ["--gpus", str(gpus)]
        proc = subprocess.Popen(cmd)
        self._procs.append(proc)
        # Record the PID in the pool-scoped registry so liveness survives fresh
        # backend instances (e.g. ensure_workers builds a throwaway backend).
        registry = Path(pool_dir) / _REGISTRY_NAME
        with open(registry, "a") as f:
            f.write(f"{proc.pid}\n")
        return WorkerHandle(backend="local", job_id=str(proc.pid), metadata={"pool_dir": pool_dir})

    def count_workers(self, pool_dir: str) -> int:
        """Number of living workers registered for this pool.

        Reads the pool-scoped ``.local_workers`` registry file (one PID per
        line), tests each PID's liveness via ``os.kill(pid, 0)``, and rewrites
        the file with only-living PIDs so it stays bounded. PID reuse is an
        acceptable risk for this local/test backend.
        """
        # Reap any of THIS instance's finished children so their zombies clear.
        for p in self._procs:
            p.poll()
        registry = Path(pool_dir) / _REGISTRY_NAME
        if not registry.exists():
            return 0
        living: list[int] = []
        for line in registry.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid = int(line)
            except ValueError:
                continue
            if _pid_alive(pid):
                living.append(pid)
        # Prune the registry to only-living PIDs.
        registry.write_text("".join(f"{pid}\n" for pid in living))
        return len(living)

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


def _pid_alive(pid: int) -> bool:
    """Test whether a PID is alive via signal 0.

    A terminated-but-unreaped child shows up as a zombie; ``os.kill(pid, 0)``
    still succeeds for zombies, so we additionally treat the ``Z`` state in
    ``/proc/<pid>/stat`` (when available) as dead.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — still alive.
        return True
    return not _pid_is_zombie(pid)


def _pid_is_zombie(pid: int) -> bool:
    """True if /proc reports the PID in the zombie state. Best-effort."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except (OSError, ValueError):
        return False
    # The state char follows the comm field, which is parenthesized and may
    # contain spaces/parens — split on the last ')'.
    rparen = stat.rfind(")")
    if rparen == -1:
        return False
    rest = stat[rparen + 1 :].split()
    return bool(rest) and rest[0] == "Z"
