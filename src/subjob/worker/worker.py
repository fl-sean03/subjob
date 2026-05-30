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
import random
import signal
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from subjob.lib.artifacts import validate_artifacts
from subjob.lib.pool import ClaimedTask, Pool
from subjob.lib.task import Task
from subjob.worker.runner import ExitResult, run_task
from subjob.worker.runner import _terminate as _terminate_proc

log = logging.getLogger("subjob.worker")

DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_WALLTIME_SAFETY = 60.0  # seconds reserved at end of allocation for cleanup
DEFAULT_MAX_ATTEMPTS = 3  # release-and-retry cap before a task is failed
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0  # how often the worker touches its heartbeat file
DEFAULT_AUTO_REAP_INTERVAL_S = 120.0  # how often a live worker sweeps for dead-owner claims
# AUTO-sweep staleness threshold = 4× heartbeat interval. Conservative: a single
# missed heartbeat (NFS hiccup, GC pause) doesn't trigger a reap; four-in-a-row
# does. Operators can override via --auto-reap-threshold / reap-stale --older-than.
DEFAULT_AUTO_REAP_THRESHOLD_MULTIPLIER = 4.0


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
        heartbeat_interval_s: float | None = DEFAULT_HEARTBEAT_INTERVAL_S,
        auto_reap_interval_s: float | None = DEFAULT_AUTO_REAP_INTERVAL_S,
        auto_reap_threshold_s: float | None = None,
    ):
        self.pool = pool
        self.caps = capabilities
        self.poll_interval = poll_interval
        self.idle_timeout_s = idle_timeout_s
        self.walltime_safety_s = walltime_safety_s
        self.heartbeat_interval_s = heartbeat_interval_s
        # auto_reap_interval_s=0 disables (parity with CLI flag convention);
        # None also disables.
        if auto_reap_interval_s is not None and auto_reap_interval_s <= 0:
            auto_reap_interval_s = None
        self.auto_reap_interval_s = auto_reap_interval_s
        # Default threshold: 4× heartbeat interval (see module comment). If
        # heartbeats are disabled, fall back to the auto-reap interval so a
        # legacy/mtime-only sweep still gets a sane window.
        if auto_reap_threshold_s is None:
            base = heartbeat_interval_s if heartbeat_interval_s else (auto_reap_interval_s or 30.0)
            auto_reap_threshold_s = base * DEFAULT_AUTO_REAP_THRESHOLD_MULTIPLIER
        self.auto_reap_threshold_s = auto_reap_threshold_s

        # Worker identity: only when heartbeats are enabled (the auto-sweep
        # depends on a stable id; without heartbeats there is nothing to read
        # against). Mutates the supplied Pool to propagate the id into claim()
        # — chosen over a setter because (i) it keeps Worker's public surface
        # small, (ii) the Pool already lives only for this Worker's lifetime,
        # and (iii) it matches how Capabilities is also mutated mid-run
        # (walltime_end). Documented in the return.
        self.worker_id: str | None = None
        if heartbeat_interval_s is not None:
            self.worker_id = (
                f"{capabilities.host or socket.gethostname()}-{os.getpid()}-"
                f"{random.randint(1000, 9999)}"
            )
            self.pool.worker_id = self.worker_id

        self._cores_free = capabilities.cores
        self._gpus_free = capabilities.gpus
        self._active: dict[Future, tuple[ClaimedTask, int, int]] = {}
        self._running_procs: dict[str, Any] = {}  # task_id → Popen, for kill-on-shutdown
        self._lock = threading.Lock()
        self._shutdown = False
        self._executor: ThreadPoolExecutor | None = None
        self._last_heartbeat: float = 0.0
        self._last_auto_reap: float = 0.0
        # One-cycle grace for unknown_dep: when a pending task references a dep
        # we can't find anywhere (done/failed/claimed/pending), we record its
        # id here and SKIP it for this dispatch cycle. Only on the SECOND
        # sighting (next poll) do we fast-fail it as `unknown_dep`. This
        # tolerates multi-process submitters that interleave deps and
        # dependents — a dep that lands within ~1 poll interval is fine.
        self._unknown_dep_seen: set[str] = set()

    # ----- main loop -----

    def run(self) -> None:
        self.pool.init()
        self._install_signal_handlers()
        # Surface the confusing zero-work case up front: if the allocation
        # budget is already at/under the safety margin, the worker will exit
        # "walltime budget exhausted" without claiming anything. Warn loudly
        # rather than silently clamping — the operator likely mis-sized the
        # allocation or the safety margin.
        if self.caps.walltime_end is not None:
            remaining = self.caps.walltime_end - time.time()
            if remaining <= self.walltime_safety_s:
                log.warning(
                    "worker has no usable time: allocation budget (%.0fs remaining) "
                    "is <= the walltime safety margin (%.0fs); it will exit without "
                    "running any tasks",
                    remaining,
                    self.walltime_safety_s,
                )
        self.pool.emit(
            "worker_started",
            "",
            {
                "host": self.caps.host,
                "cores": self.caps.cores,
                "worker_id": self.worker_id,
            },
        )
        # First heartbeat before the loop starts: a sibling worker that polls
        # auto-reap between our claim and our first in-loop tick should still
        # see us as alive.
        self._maybe_heartbeat(force=True)
        self._executor = ThreadPoolExecutor(max_workers=max(self.caps.cores, 1))
        try:
            self._loop()
        finally:
            self._drain_or_release()
            if self._executor is not None:
                self._executor.shutdown(wait=True)
            if self.worker_id is not None:
                # Best-effort cleanup; never block shutdown on a missing file
                # or permission glitch.
                try:
                    self.pool.remove_heartbeat(self.worker_id)
                except OSError:
                    log.exception("failed to remove heartbeat file on shutdown")
            self.pool.emit("worker_stopped", "", {"host": self.caps.host})

    def _loop(self) -> None:
        last_activity = time.time()
        while not self._shutdown:
            if self._walltime_expired():
                log.info("walltime budget exhausted; stopping")
                # Mirror the signal handler: mark shutdown so in-flight tasks
                # take the release (retry) path in _run_one rather than being
                # recorded as failures. A walltime-preempted task is healthy,
                # just out of time — it must be re-queued, not failed.
                self._shutdown = True
                break
            self._maybe_heartbeat()
            self._maybe_auto_reap()
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

    # ----- heartbeat / auto-reap -----

    def _maybe_heartbeat(self, *, force: bool = False) -> None:
        if self.worker_id is None or self.heartbeat_interval_s is None:
            return
        now = time.time()
        if not force and (now - self._last_heartbeat) < self.heartbeat_interval_s:
            return
        try:
            self.pool.write_heartbeat(self.worker_id)
        except OSError:
            # Filesystem flap shouldn't kill the loop; we'll retry next tick.
            log.exception("heartbeat write failed for %s", self.worker_id)
            return
        self._last_heartbeat = now

    def _maybe_auto_reap(self) -> None:
        if self.auto_reap_interval_s is None:
            return
        now = time.time()
        if (now - self._last_auto_reap) < self.auto_reap_interval_s:
            return
        self._last_auto_reap = now
        try:
            results = self.pool.auto_reap_stale(
                threshold_s=self.auto_reap_threshold_s,
                skip_worker_id=self.worker_id,
                now=now,
            )
        except OSError:
            log.exception("auto-reap sweep failed; will retry next interval")
            return
        if results:
            log.info("auto-reaped %d stale claim(s): %s", len(results), results)

    # ----- dispatch / reap -----

    def _dispatch_pending(self) -> int:
        claimed_count = 0
        # pending_tasks() returns cached (path, Task) pairs — no re-read here.
        # Corrupt-YAML quarantine is handled inside pending_tasks().
        pending = self.pool.pending_tasks()
        # Prune the unknown-dep grace tracker so we don't accumulate ids
        # across submissions — once a task leaves pending/ it's no longer
        # relevant. This also handles the case where the dep landed and the
        # dependent moved on cleanly.
        pending_ids_for_prune = {p.stem for p, _ in pending}
        self._unknown_dep_seen &= pending_ids_for_prune
        # DAG dep-state lookup: compute terminal-state sets ONCE per dispatch
        # cycle so a fan-in/fan-out batch doesn't restat the same dirs per task.
        # Per-task pre-claim gate (Phase-1 pull-forward) — no graph datastructure
        # needed; the dispatch loop already iterates each pending peek.
        done_ids: set[str] | None = None
        failed_ids: set[str] | None = None
        claimed_ids: set[str] | None = None
        pending_ids: set[str] | None = None

        def _dep_sets() -> tuple[set[str], set[str], set[str], set[str]]:
            nonlocal done_ids, failed_ids, claimed_ids, pending_ids
            if done_ids is None:
                done_ids = {p.stem for p in self.pool.list_state("done")}
                failed_ids = {p.stem for p in self.pool.list_state("failed")}
                claimed_ids = {p.stem for p in self.pool.list_state("claimed")}
                pending_ids = {p.stem for p, _ in pending}
            return done_ids, failed_ids, claimed_ids, pending_ids  # type: ignore[return-value]

        for path, peek in pending:
            # Cap infinite walltime-bounce: a task repeatedly released without
            # completing (e.g. it needs more time than any worker has) is
            # failed rather than re-run forever.
            max_attempts = int(peek.retry.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
            if self.pool.release_attempt_count(peek) >= max_attempts:
                claimed = self.pool.claim(path)
                if claimed is not None:
                    self.pool.commit_failed(
                        claimed,
                        {
                            "error": f"exceeded max_attempts ({max_attempts}) "
                            "without completing — likely needs more walltime than available",
                            "host": self.caps.host,
                        },
                    )
                continue
            # DAG gate: a task with unmet deps either waits, cascades-failed
            # (dep already failed), or fails-fast (dep is unknown / never
            # submitted). Sits between max_attempts and the capacity check so
            # capacity isn't burned on tasks that can't run yet.
            if peek.depends_on:
                done_s, failed_s, claimed_s, pending_s = _dep_sets()
                dep_action: tuple[str, str] | None = None  # ("cascade"|"unknown", dep_id)
                deps_pending = False
                for dep_id in peek.depends_on:
                    if dep_id in done_s:
                        continue
                    if dep_id in failed_s:
                        dep_action = ("cascade", dep_id)
                        break
                    if dep_id in pending_s or dep_id in claimed_s:
                        deps_pending = True
                        continue
                    # Not terminal, not in-flight, not queued — typo or
                    # never-submitted. One-cycle grace: skip this cycle and
                    # only fast-fail if we still can't find it next cycle.
                    # See self._unknown_dep_seen (in __init__) for rationale.
                    dep_action = ("unknown", dep_id)
                    break
                if dep_action is not None:
                    kind, dep_id = dep_action
                    if kind == "unknown" and peek.id not in self._unknown_dep_seen:
                        # First sighting: record + skip. The dep may yet
                        # arrive (multi-process submitters often interleave).
                        self._unknown_dep_seen.add(peek.id)
                        continue
                    claimed = self.pool.claim(path)
                    if claimed is not None:
                        if kind == "cascade":
                            payload = {
                                "dep_failed": dep_id,
                                "error": f"dependency {dep_id!r} failed",
                                "host": self.caps.host,
                            }
                        else:
                            payload = {
                                "unknown_dep": dep_id,
                                "error": f"depends_on references unknown task {dep_id!r}",
                                "host": self.caps.host,
                            }
                        self.pool.commit_failed(claimed, payload)
                    continue
                if deps_pending:
                    # Deps haven't reached a terminal state yet — leave the task
                    # in pending/ and re-evaluate next cycle. Don't burn a claim.
                    continue
            with self._lock:
                # No free cores at all → nothing more can be claimed this cycle.
                if self._cores_free <= 0:
                    break
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
                # A finalize failure (ENOSPC, missing dir, etc.) must not unwind
                # the loop and leak every other in-flight claim — survive it.
                try:
                    self.pool.commit_failed(claim, {"error": f"runner crashed: {e}"})
                except Exception:
                    log.exception("commit_failed raised for task %s; continuing", claim.task.id)

    def _run_one(self, claim: ClaimedTask) -> None:
        self.pool.emit("task_started", claim.task.id, {"host": self.caps.host})

        def _register(proc):
            with self._lock:
                self._running_procs[claim.task.id] = proc

        result: ExitResult = run_task(
            claim.task, self.pool.logs_dir, host=self.caps.host, on_spawn=_register
        )
        with self._lock:
            self._running_procs.pop(claim.task.id, None)

        if result.succeeded:
            # Exit code 0 alone isn't enough: validate any declared artifacts
            # before recording success. A command that exits clean without
            # writing its outputs is a real failure, not a silent wrong result.
            ok, detail = validate_artifacts(
                claim.task.artifacts,
                env=claim.task.env,
                workdir=claim.task.workdir or None,
            )
            payload = result.to_dict()
            if ok:
                payload["artifacts"] = detail
                self.pool.commit_done(claim, payload)
            else:
                payload["artifact_validation_failed"] = True
                payload["artifact_detail"] = detail
                self.pool.commit_failed(claim, payload)
        elif self._shutdown:
            # We were told to stop and the task didn't finish cleanly — release
            # it (back to pending) so another worker retries, rather than
            # recording a spurious failure. This is the clean-preemption path.
            self.pool.release(claim, reason="worker_shutdown")
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

    def _drain_or_release(self, grace_s: float = 5.0) -> None:
        """On shutdown, give running tasks a brief grace to finish, then kill
        their subprocesses so they don't orphan or double-run.

        Each task thread releases its OWN claim (see _run_one's shutdown
        branch) once its subprocess exits — so this method only needs to (a)
        wait briefly for natural completion and (b) terminate stragglers.
        executor.shutdown(wait=True) in run() then joins the threads quickly.
        """
        if not self._active:
            return
        deadline = time.time() + grace_s
        while self._active and time.time() < deadline:
            self._reap_finished()
            time.sleep(0.1)
        # Kill any still-running subprocesses; their threads will then release.
        with self._lock:
            procs = list(self._running_procs.items())
        for task_id, proc in procs:
            log.info("terminating in-flight task %s on shutdown", task_id)
            _terminate_proc(proc)

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
