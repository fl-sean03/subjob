"""Run one Task as a subprocess. Capture logs, enforce walltime, report.

The runner has no knowledge of the pool or claim machinery — it just takes
a Task plus a logs directory and runs the command. Walltime is the task's
own declared limit; the worker is responsible for not starting a task
when its declared walltime won't fit in the remaining allocation.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from subjob.lib.task import Task

_TAIL_BYTES = 4096


@dataclass
class ExitResult:
    task_id: str
    exit_code: int
    duration_s: float
    started_at: str
    finished_at: str
    host: str
    stdout_tail: str = ""
    stderr_tail: str = ""
    walltime_killed: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "task_id": self.task_id,
            "exit_code": self.exit_code,
            "duration_s": round(self.duration_s, 3),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "host": self.host,
            "walltime_killed": self.walltime_killed,
        }
        if self.stdout_tail:
            d["stdout_tail"] = self.stdout_tail
        if self.stderr_tail:
            d["stderr_tail"] = self.stderr_tail
        if self.error:
            d["error"] = self.error
        return d

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.walltime_killed and not self.error


def run_task(task: Task, logs_dir: Path, host: str | None = None) -> ExitResult:
    host = host or socket.gethostname()
    stdout_path = logs_dir / f"{task.id}.out"
    stderr_path = logs_dir / f"{task.id}.err"
    logs_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update({k: str(v) for k, v in task.env.items()})

    started_wall = time.time()
    started_at = _iso(started_wall)

    walltime = task.resources.walltime_seconds
    walltime_killed = False
    error = ""
    rc = -1

    try:
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            proc = subprocess.Popen(  # noqa: S602 — task commands are intentionally shell strings
                task.command,
                shell=True,
                stdout=out,
                stderr=err,
                env=env,
            )
            try:
                rc = proc.wait(timeout=walltime if walltime > 0 else None)
            except subprocess.TimeoutExpired:
                _terminate(proc)
                walltime_killed = True
                rc = -1
    except OSError as e:
        error = f"failed to spawn: {e}"
        rc = -1

    finished_wall = time.time()
    return ExitResult(
        task_id=task.id,
        exit_code=rc,
        duration_s=finished_wall - started_wall,
        started_at=started_at,
        finished_at=_iso(finished_wall),
        host=host,
        stdout_tail=_tail(stdout_path),
        stderr_tail=_tail(stderr_path),
        walltime_killed=walltime_killed,
        error=error,
    )


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort kill: SIGTERM, brief wait, then SIGKILL."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    except ProcessLookupError:
        pass


def _tail(path: Path, n_bytes: int = _TAIL_BYTES) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    if size == 0:
        return ""
    with open(path, "rb") as f:
        if size > n_bytes:
            f.seek(-n_bytes, os.SEEK_END)
        return f.read().decode("utf-8", errors="replace")


def _iso(wall_time: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall_time))
