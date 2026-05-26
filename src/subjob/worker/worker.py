"""Worker — the pilot process. Pulls tasks from a Pool and runs them.

A worker:
  - Detects its own capabilities (cores, hostname, walltime budget)
  - Polls the pool for pending tasks it can run
  - Atomically claims them, runs them, commits results
  - Releases unfinished claims if it has to shut down

Concurrency: tasks run on a ThreadPoolExecutor; each one spawns its own
subprocess. The worker tracks `cores_free` and refuses to start a task
whose resource ask exceeds the current free pool.

Walltime: the worker keeps a `walltime_end` epoch (from SLURM env or CLI)
and refuses to start a task whose declared walltime won't fit before that
deadline (minus a safety margin).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from subjob.lib.pool import ClaimedTask, Pool
from subjob.lib.task import Task
from subjob.worker.runner import ExitResult, run_task

log = logging.getLogger("subjob.worker")

DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_WALLTIME_SAFETY = 60.0  # seconds reserved at end of allocation for cleanup


@dataclass
class Capabilities:
    cores: int
    gpus: int = 0
    host: str = ""
    walltime_end: float | None = None  # epoch seconds, None = unlimited

    @classmethod
    def from_env(
        cls,
        *,
        cores: int | None = None,
        gpus: int | None = None,
        walltime_seconds: int | None = None,
    ) -> Capabilities:
        env = os.environ
        if cores is None:
            cores = int(env.get("SLURM_CPUS_ON_NODE") or env.get("SLURM_NTASKS") or 1)
        if gpus is None:
            gpus = int(env.get("SLURM_GPUS") or env.get("SLURM_GPUS_ON_NODE") or 0)
        walltime_end = _compute_walltime_end(walltime_seconds)
        return cls(cores=cores, gpus=gpus, host=socket.gethostname(), walltime_end=walltime_end)


def _compute_walltime_end(cli_seconds: int | None) -> float | None:
    """Resolve the walltime deadline (epoch seconds) from SLURM env or a CLI flag."""
    env = os.environ
    end_env = env.get("SLURM_JOB_END_TIME")
    if end_env:
        try:
            return float(end_env)
        except ValueError:
            pass
    start_env = env.get("SLURM_JOB_START_TIME")
    limit_env = env.get("SLURM_JOB_TIMELIMIT") or env.get("SLURM_TIMELIMIT")
    if start_env and limit_env:
        try:
            return float(start_env) + _parse_timelimit(limit_env)
        except (ValueError, TypeError):
            pass
    if cli_seconds is not None and cli_seconds > 0:
        return time.time() + cli_seconds
    return None


def _parse_timelimit(s: str) -> float:
    """Parse a SLURM TIMELIMIT string: 'd-hh:mm:ss', 'hh:mm:ss', 'mm:ss', or 'minutes'."""
    s = s.strip()
    if "-" in s:
        days, rest = s.split("-", 1)
        days = int(days)
    else:
        days, rest = 0, s
    parts = rest.split(":")
    if len(parts) == 3:
        h, m, sec = (int(x) for x in parts)
    elif len(parts) == 2:
        h, m, sec = 0, int(parts[0]), int(parts[1])
    elif len(parts) == 1:
        h, m, sec = 0, int(parts[0]), 0
    else:
        raise ValueError(f"bad SLURM timelimit: {s}")
    return days * 86400 + h * 3600 + m * 60 + sec


class Worker:
    def __init__(
        self,
        pool: Pool,
        capabilities: Capabilities,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        idle_timeout_s: float | None = None,
        walltime_safety_s: float = DEFAULT_WALLTIME_SAFETY,
    ):
        self.pool = pool
        self.caps = capabilities
        self.poll_interval = poll_interval
        self.idle_timeout_s = idle_timeout_s
        self.walltime_safety_s = walltime_safety_s

        self._cores_free = capabilities.cores
        self._gpus_free = capabilities.gpus
        self._active: dict[Future, tuple[ClaimedTask, int, int]] = {}
        self._lock = threading.Lock()
        self._shutdown = False
        self._executor: ThreadPoolExecutor | None = None

    # ----- main loop -----

    def run(self) -> None:
        self.pool.init()
        self._install_signal_handlers()
        self.pool.emit("worker_started", "", {"host": self.caps.host, "cores": self.caps.cores})
        self._executor = ThreadPoolExecutor(max_workers=max(self.caps.cores, 1))
        try:
            self._loop()
        finally:
            self._drain_or_release()
            if self._executor is not None:
                self._executor.shutdown(wait=True)
            self.pool.emit("worker_stopped", "", {"host": self.caps.host})

    def _loop(self) -> None:
        last_activity = time.time()
        while not self._shutdown:
            if self._walltime_expired():
                log.info("walltime budget exhausted; stopping")
                break
            self._reap_finished()
            claimed_any = self._dispatch_pending()
            with self._lock:
                if claimed_any or self._active:
                    last_activity = time.time()
            if self._should_exit_idle(last_activity):
                break
            time.sleep(self.poll_interval)
        # Final reap so anything that finished during the last sleep is committed.
        self._reap_finished()

    # ----- dispatch / reap -----

    def _dispatch_pending(self) -> int:
        claimed_count = 0
        for path in self.pool.pending_paths():
            try:
                peek = Task.read(path)
            except Exception:
                continue
            with self._lock:
                if peek.resources.cores > self._cores_free:
                    continue
                if peek.resources.gpus > self._gpus_free:
                    continue
            if not self._task_fits_walltime(peek):
                continue
            claimed = self.pool.claim(path)
            if claimed is None:
                continue
            cores = claimed.task.resources.cores
            gpus = claimed.task.resources.gpus
            with self._lock:
                self._cores_free -= cores
                self._gpus_free -= gpus
            self.pool.emit(
                "task_claimed",
                claimed.task.id,
                {"host": self.caps.host, "cores": cores, "gpus": gpus},
            )
            assert self._executor is not None
            future = self._executor.submit(self._run_one, claimed)
            with self._lock:
                self._active[future] = (claimed, cores, gpus)
            claimed_count += 1
        return claimed_count

    def _reap_finished(self) -> None:
        with self._lock:
            done = [f for f in self._active if f.done()]
        for f in done:
            with self._lock:
                claim, cores, gpus = self._active.pop(f)
                self._cores_free += cores
                self._gpus_free += gpus
            try:
                f.result()  # surface any unexpected exception in the runner
            except Exception as e:
                log.exception("runner crashed for task %s", claim.task.id)
                self.pool.commit_failed(claim, {"error": f"runner crashed: {e}"})

    def _run_one(self, claim: ClaimedTask) -> None:
        self.pool.emit("task_started", claim.task.id, {"host": self.caps.host})
        result: ExitResult = run_task(claim.task, self.pool.logs_dir, host=self.caps.host)
        if result.succeeded:
            self.pool.commit_done(claim, result.to_dict())
        else:
            self.pool.commit_failed(claim, result.to_dict())

    # ----- shutdown / walltime -----

    def _walltime_expired(self) -> bool:
        if self.caps.walltime_end is None:
            return False
        return time.time() + self.walltime_safety_s >= self.caps.walltime_end

    def _task_fits_walltime(self, task: Task) -> bool:
        if self.caps.walltime_end is None:
            return True
        remaining = self.caps.walltime_end - time.time() - self.walltime_safety_s
        return task.resources.walltime_seconds <= remaining

    def _should_exit_idle(self, last_activity: float) -> bool:
        if self.idle_timeout_s is None:
            return False
        with self._lock:
            if self._active:
                return False
        return time.time() - last_activity > self.idle_timeout_s

    def _drain_or_release(self) -> None:
        """Wait briefly for in-flight tasks, then release any still-claimed ones."""
        if not self._active:
            return
        # Give running tasks a chance to finish naturally before yanking the rug.
        deadline = time.time() + 5.0
        while self._active and time.time() < deadline:
            self._reap_finished()
            time.sleep(0.1)
        with self._lock:
            stragglers = list(self._active.items())
        for future, (claim, _cores, _gpus) in stragglers:
            if not future.done():
                # We can't preempt the thread cleanly; the subprocess will keep
                # running until the SLURM allocation hard-kills it. Release the
                # claim so a future worker can retry.
                self.pool.release(claim)
                with self._lock:
                    self._active.pop(future, None)

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.info("received signal %s; shutting down", signum)
            self._shutdown = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Not on main thread (e.g., inside tests with threads) — skip.
                pass
