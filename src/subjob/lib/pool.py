"""Pool — directory-on-shared-FS that brokers tasks between submitters and workers.

Layout (under `<pool>/`):
    pending/<id>.yaml      task files waiting to be claimed
    claimed/<id>.yaml      a worker has claimed but not yet finished
    done/<id>.yaml         finished successfully
    failed/<id>.yaml       finished with non-zero exit or other failure
    logs/<id>.out          per-task stdout
    logs/<id>.err          per-task stderr
    .staging/              short-lived; used for atomic write-then-rename of new tasks
    journal.jsonl          append-only event stream (one JSON object per line)

All transitions between state directories use os.rename via lib.lock.atomic_move.
The journal is append-only; readers tail it.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from subjob.lib.lock import atomic_move
from subjob.lib.task import Task

try:
    import fcntl  # POSIX only; used to serialize journal appends across writers

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX fallback
    _HAVE_FCNTL = False

STATES = ("pending", "claimed", "done", "failed")

# Per-pool dir holding `<worker_id>` files, each containing a single
# `<unix_ns_timestamp>` line written atomically (tmp + os.rename). Hidden
# subdir, mirroring `.local_workers`/`.staging` precedent.
HEARTBEATS_DIRNAME = ".heartbeats"


@dataclass
class ClaimedTask:
    """A task plus its current on-disk path (under claimed/)."""

    task: Task
    path: Path


class Pool:
    def __init__(self, root: Path | str, *, worker_id: str | None = None):
        self.root = Path(root)
        self.pending_dir = self.root / "pending"
        self.claimed_dir = self.root / "claimed"
        self.done_dir = self.root / "done"
        self.failed_dir = self.root / "failed"
        self.logs_dir = self.root / "logs"
        self.staging_dir = self.root / ".staging"
        self.heartbeats_dir = self.root / HEARTBEATS_DIRNAME
        self.journal_path = self.root / "journal.jsonl"
        # Optional worker identity. When set, claim() stamps a `claimed_by`
        # attempt entry so reap-stale --auto knows which worker owns each
        # claim. Default-None preserves bit-exact pre-thrust behavior.
        self.worker_id = worker_id
        # Cache of parsed pending tasks keyed by filename → (mtime, Task).
        # Validated by mtime so a re-written file (e.g. a released task that
        # recorded an attempt) is re-read rather than served stale. Turns the
        # per-poll cost from O(N reads) to O(N stat + changed-file reads).
        # See validation/PLAN.md F-001.
        self._pending_cache: dict[str, tuple[float, Task]] = {}

    # ----- lifecycle -----

    def init(self) -> None:
        """Create the pool layout. Idempotent."""
        for d in (
            self.pending_dir,
            self.claimed_dir,
            self.done_dir,
            self.failed_dir,
            self.logs_dir,
            self.staging_dir,
            self.heartbeats_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        self.journal_path.touch(exist_ok=True)

    # ----- submit -----

    def submit(self, task: Task) -> str:
        """Atomically place a single task in pending/. Returns the task id.

        Uses an exclusive hardlink (os.link) into pending/ so two concurrent
        submitters racing on the same task id can't clobber each other — the
        loser gets FileExistsError, which we surface as a duplicate-id error.
        The soft _task_exists_anywhere check still gives a friendly error for
        ids already in claimed/done/failed.
        """
        self.init()
        if self._task_exists_anywhere(task.id):
            raise ValueError(f"task id already in pool: {task.id!r}")
        text = task.to_yaml()
        # Write to staging, then exclusive-link into pending/.
        tmp_path = self._stage_write(task.id, text)
        target = self.pending_dir / f"{task.id}.yaml"
        try:
            os.link(tmp_path, target)  # atomic; raises FileExistsError if id taken
        except FileExistsError:
            os.unlink(tmp_path)
            raise ValueError(f"task id already in pool: {task.id!r}") from None
        finally:
            # Drop the staging link; the pending/ link remains.
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
        self.emit("task_submitted", task.id, {"priority": task.priority})
        return task.id

    def submit_batch(self, tasks: Iterable[Task]) -> list[str]:
        return [self.submit(t) for t in tasks]

    def _stage_write(self, task_id: str, text: str) -> Path:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{task_id}.", suffix=".yaml", dir=self.staging_dir)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
        except Exception:
            os.unlink(tmp_name)
            raise
        return Path(tmp_name)

    def _task_exists_anywhere(self, task_id: str) -> bool:
        name = f"{task_id}.yaml"
        for d in (self.pending_dir, self.claimed_dir, self.done_dir, self.failed_dir):
            if (d / name).exists():
                return True
        return False

    # ----- status / inspection -----

    def status(self) -> dict[str, int]:
        return {s: self._count(s) for s in STATES}

    def _count(self, state: str) -> int:
        d = self.root / state
        if not d.exists():
            return 0
        return sum(1 for p in d.iterdir() if p.suffix == ".yaml")

    def list_state(self, state: str) -> list[Path]:
        d = self.root / state
        if not d.exists():
            return []
        return sorted(p for p in d.iterdir() if p.suffix == ".yaml")

    def read_task(self, state: str, task_id: str) -> Task:
        return Task.read(self.root / state / f"{task_id}.yaml")

    # ----- claim / release / commit -----

    def pending_tasks(self) -> list[tuple[Path, Task]]:
        """Return (path, Task) for each pending file, priority-sorted.

        Uses a per-Pool cache so each immutable pending YAML is parsed at
        most once across the lifetime of this Pool object (the worker's
        poll loop reuses one Pool). Unreadable YAMLs are quarantined to
        failed/ so they don't poison-pill the loop.
        """
        items: list[tuple[int, float, Path, Task]] = []
        live_names: set[str] = set()
        for p in self.pending_dir.iterdir():
            if p.suffix != ".yaml":
                continue
            name = p.name
            try:
                mtime = p.stat().st_mtime
            except OSError:
                # File vanished (claimed by another worker between listing
                # and stat) — skip it this cycle.
                continue
            live_names.add(name)
            cached = self._pending_cache.get(name)
            if cached is not None and cached[0] == mtime:
                task = cached[1]
            else:
                try:
                    task = Task.read(p)
                except Exception as e:
                    dest = self.failed_dir / name
                    try:
                        os.rename(p, dest)
                        self.emit(
                            "task_failed",
                            p.stem,
                            {"error": f"unreadable pending YAML: {e}"},
                        )
                    except OSError:
                        pass
                    continue
                self._pending_cache[name] = (mtime, task)
            items.append((-task.priority, mtime, p, task))
        # Evict cache entries for files no longer pending (claimed/done).
        for stale in self._pending_cache.keys() - live_names:
            del self._pending_cache[stale]
        items.sort(key=lambda it: (it[0], it[1]))
        return [(p, t) for _, _, p, t in items]

    def pending_paths(self) -> list[Path]:
        """Return pending task files, priority-sorted. Thin wrapper over pending_tasks()."""
        return [p for p, _ in self.pending_tasks()]

    def claim(self, pending_path: Path) -> ClaimedTask | None:
        """Try to claim a pending task. Returns the loaded task or None on race loss.

        If ``self.worker_id`` is set, records an attempt entry
        ``{"claimed_by": worker_id, "claimed_at": <iso>, "host": <hostname>}``
        on the YAML BEFORE returning. This adds one file rewrite per claim but
        gives reap-stale --auto a positive owner-identification signal so
        recovery doesn't have to guess from mtime alone.
        """
        target = self.claimed_dir / pending_path.name
        if not atomic_move(pending_path, target):
            return None
        try:
            task = Task.read(target)
        except Exception:
            # Corrupt YAML in claimed/ — bubble it out so the worker can decide.
            atomic_move(target, self.failed_dir / target.name)
            raise
        if self.worker_id is not None:
            stamp = {
                "claimed_by": self.worker_id,
                "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "host": socket.gethostname(),
            }
            task.attempts = list(task.attempts) + [stamp]
            try:
                target.write_text(task.to_yaml())
            except OSError:
                # If the stamp write fails, the claim is still valid (the file
                # is in claimed/); recovery just falls back to mtime for this
                # task. Don't fail the claim over a rewrite hiccup.
                pass
        return ClaimedTask(task=task, path=target)

    def release(self, claimed: ClaimedTask, reason: str = "") -> bool:
        """Move a claimed task back to pending, recording a release attempt.

        Records an attempt marker in the YAML before moving so a re-claiming
        worker can see how many times this task has been released (used to
        cap infinite walltime-bounce — see Worker._dispatch_pending).
        """
        # If the claim was already finalized (moved out of claimed/) by another
        # path, do nothing — never re-create the file or resurrect the task.
        if not claimed.path.exists():
            return False
        task = claimed.task
        task.attempts = list(task.attempts) + [
            {"released": True, "reason": reason or "worker_shutdown"}
        ]
        try:
            claimed.path.write_text(task.to_yaml())
        except OSError:
            pass
        dest = self.pending_dir / claimed.path.name
        if atomic_move(claimed.path, dest):
            # Invalidate any stale cache entry; the file's content changed.
            self._pending_cache.pop(claimed.path.name, None)
            self.emit("task_released", task.id, {"reason": reason or "worker_shutdown"})
            return True
        return False

    # ----- heartbeats -----

    def write_heartbeat(self, worker_id: str, now_ns: int | None = None) -> None:
        """Atomically refresh ``<pool>/.heartbeats/<worker_id>``.

        Writes a single line containing the unix-ns timestamp via a
        tmp-file + os.rename so a concurrent reader can never get a partial
        value. Creates the heartbeats dir on demand so this is safe to call
        before init() (it isn't normally, but be defensive).
        """
        self.heartbeats_dir.mkdir(parents=True, exist_ok=True)
        ts = time.time_ns() if now_ns is None else now_ns
        target = self.heartbeats_dir / worker_id
        # Use a sibling tmp file so os.rename stays on one filesystem.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{worker_id}.", suffix=".hb", dir=self.heartbeats_dir
        )
        try:
            os.write(fd, f"{ts}\n".encode())
        finally:
            os.close(fd)
        os.replace(tmp_name, target)

    def read_heartbeat_ns(self, worker_id: str) -> int | None:
        """Return the heartbeat timestamp (ns) for a worker, or None if absent."""
        path = self.heartbeats_dir / worker_id
        try:
            text = path.read_text().strip()
        except OSError:
            return None
        try:
            return int(text.split()[0]) if text else None
        except (ValueError, IndexError):
            return None

    def remove_heartbeat(self, worker_id: str) -> None:
        """Best-effort heartbeat-file removal (called from worker shutdown)."""
        try:
            (self.heartbeats_dir / worker_id).unlink()
        except OSError:
            pass

    @staticmethod
    def _last_claim_stamp(task: Task) -> dict[str, Any] | None:
        """Return the most recent claim-stamp attempt for a task, or None."""
        for a in reversed(task.attempts):
            if "claimed_by" in a:
                return a
        return None

    def auto_reap_stale(
        self,
        *,
        threshold_s: float,
        skip_worker_id: str | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Sweep ``claimed/`` and release tasks whose owner is gone or stale.

        Behavior:
          - If a claimed task has a ``claimed_by`` stamp matching
            ``skip_worker_id``, it is skipped (a worker never reaps itself).
          - If it has a ``claimed_by`` with no heartbeat file → reap with
            reason ``owner_missing``.
          - If it has a ``claimed_by`` with a heartbeat older than
            ``threshold_s`` → reap with reason ``owner_stale_<N>s``.
          - If it has NO ``claimed_by`` (legacy pre-thrust claim) → fall back
            to file mtime; reap when ``now - mtime > threshold_s`` with reason
            ``legacy_mtime_<N>s``.

        Reaped tasks are moved back to ``pending/`` with a ``reaped_stale``
        attempt entry and a ``task_released`` journal event. Returns one dict
        per acted-on task with fields ``task_id``, ``reason``, ``moved_to``.
        """
        now = time.time() if now is None else now
        results: list[dict[str, Any]] = []
        if not self.claimed_dir.exists():
            return results
        for p in sorted(self.claimed_dir.iterdir()):
            if p.suffix != ".yaml":
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            try:
                task = Task.read(p)
            except Exception:
                # Corrupt claimed/ YAML — leave it for the operator path.
                continue
            stamp = self._last_claim_stamp(task)
            reason: str | None = None
            if stamp is not None:
                owner = stamp.get("claimed_by")
                if skip_worker_id is not None and owner == skip_worker_id:
                    continue
                hb_ns = self.read_heartbeat_ns(owner) if owner else None
                if hb_ns is None:
                    reason = "owner_missing"
                else:
                    age_s = now - (hb_ns / 1e9)
                    if age_s > threshold_s:
                        reason = f"owner_stale_{int(age_s)}s"
            else:
                age_s = now - mtime
                if age_s > threshold_s:
                    reason = f"legacy_mtime_{int(age_s)}s"
            if reason is None:
                continue
            # Reap: record the attempt, then atomic-move back to pending/.
            task.attempts = list(task.attempts) + [
                {"reaped_stale": True, "reason": reason}
            ]
            try:
                p.write_text(task.to_yaml())
            except OSError:
                continue
            dest = self.pending_dir / p.name
            if atomic_move(p, dest):
                self._pending_cache.pop(p.name, None)
                self.emit(
                    "task_released",
                    p.stem,
                    {"reaped_stale": True, "reason": reason},
                )
                results.append(
                    {"task_id": p.stem, "reason": reason, "moved_to": "pending"}
                )
        return results

    @staticmethod
    def release_attempt_count(task: Task) -> int:
        """How many times this task has been released without completing."""
        return sum(1 for a in task.attempts if a.get("released"))

    def commit_done(self, claimed: ClaimedTask, payload: dict[str, Any]) -> None:
        self._finalize(claimed, "done", "task_done", payload)

    def commit_failed(self, claimed: ClaimedTask, payload: dict[str, Any]) -> None:
        self._finalize(claimed, "failed", "task_failed", payload)

    def _finalize(self, claimed: ClaimedTask, state: str, event_type: str, payload: dict) -> None:
        # If the claim was already finalized (moved out of claimed/) by another
        # path, do nothing — re-writing the file here would resurrect the task
        # into a second state dir (or re-run it). The first finalize wins.
        if not claimed.path.exists():
            return
        task = claimed.task
        task.state = state
        task.attempts = list(task.attempts) + [payload]
        # Rewrite the YAML in-place under claimed/, then move it atomically.
        claimed.path.write_text(task.to_yaml())
        dest_dir = self.done_dir if state == "done" else self.failed_dir
        os.rename(claimed.path, dest_dir / claimed.path.name)
        self.emit(event_type, task.id, payload)

    # ----- journal -----

    def emit(self, event_type: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            # event_id is a nanosecond timestamp that serves three roles: the
            # journal cursor (read_journal/follow compare with strict `>`), an
            # ordering key, AND a coarse clock for validation's throughput gate
            # (which divides event_id deltas by 1e9 assuming ns). Keep it a
            # plain ns count so all three stay calibrated. Two emits on
            # different nodes landing in the same ns would collide on the
            # cursor — accepted Phase-0 limitation (astronomically rare; node
            # clocks aren't ns-synced anyway).
            "event_id": time.time_ns(),
            "type": event_type,
            "task_id": task_id,
            "payload": payload,
        }
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(event) + "\n").encode("utf-8")
        # Serialize appends across all writers (threads in one worker AND
        # separate worker processes / nodes sharing this journal). flock is
        # advisory but honored by every subjob writer; combined with a single
        # os.write it keeps lines from interleaving on shared filesystems.
        fd = os.open(self.journal_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            if _HAVE_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                os.write(fd, line)
            finally:
                if _HAVE_FCNTL:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return event

    def read_journal(self, since_event_id: int = 0) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        out: list[dict] = []
        with open(self.journal_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    # Tolerate a rare torn line from concurrent appends rather
                    # than failing the whole read. flock makes this unlikely.
                    continue
                if ev.get("event_id", 0) > since_event_id:
                    out.append(ev)
        return out

    def follow_until_done(
        self,
        timeout_s: float | None = None,
        poll_interval: float = 2.0,
    ) -> dict[str, int]:
        """Block until no tasks remain pending or claimed; return final status().

        Polls status() and sleeps poll_interval between checks. If timeout_s is
        given and elapses while work is still outstanding (pending or claimed > 0),
        raises TimeoutError carrying the last status snapshot.
        """
        end_at = (time.time() + timeout_s) if timeout_s is not None else None
        while True:
            status = self.status()
            if status["pending"] == 0 and status["claimed"] == 0:
                return status
            if end_at is not None and time.time() >= end_at:
                raise TimeoutError(f"timed out waiting for pool to drain: {status}")
            time.sleep(poll_interval)

    def follow_until_state(
        self,
        state: str,
        task_ids: list[str],
        timeout_s: float | None = None,
        poll_interval: float = 2.0,
    ) -> bool:
        """Block until every id in task_ids is terminal (in done/ or failed/).

        Returns True iff all ids ended up in the requested ``state`` dir.
        Raises TimeoutError if timeout_s elapses before all ids are terminal.
        """
        wanted = set(task_ids)
        end_at = (time.time() + timeout_s) if timeout_s is not None else None
        while True:
            done = {p.stem for p in self.list_state("done")}
            failed = {p.stem for p in self.list_state("failed")}
            terminal = done | failed
            if wanted <= terminal:
                target = done if state == "done" else failed
                return wanted <= target
            if end_at is not None and time.time() >= end_at:
                outstanding = sorted(wanted - terminal)
                raise TimeoutError(
                    f"timed out waiting for {len(outstanding)} task(s) to reach a "
                    f"terminal state: {outstanding}"
                )
            time.sleep(poll_interval)

    def ensure_workers(self, backend: Any, count: int, **worker_kwargs: Any) -> list:
        """Ensure at least ``count`` workers are running against this pool.

        ``backend`` may be a Backend instance or the string "slurm"/"local"
        (resolved via subjob.backends.make_backend). Submits only the shortfall
        (count - currently-running) and returns the list of new WorkerHandles.

        Phase-0 liveness is best-effort: ``count_workers`` reflects what the
        backend can observe (squeue for SLURM, live subprocesses for local) and
        does not guarantee a worker is actually polling the pool yet.
        """
        if isinstance(backend, str):
            # Lazy import to avoid any import-order surprises (backends import
            # nothing from pool, but keep this defensive).
            from subjob.backends import make_backend

            backend = make_backend(backend)
        running = backend.count_workers(str(self.root))
        shortfall = max(0, count - running)
        handles = []
        for _ in range(shortfall):
            handles.append(backend.submit_worker(pool_dir=str(self.root), **worker_kwargs))
        return handles

    def follow(
        self,
        timeout_s: float | None = None,
        since_event_id: int = 0,
        poll_interval: float = 0.1,
    ) -> Iterator[dict[str, Any]]:
        """Yield events as they appear in the journal. Stops at timeout."""
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.touch(exist_ok=True)
        end_at = (time.time() + timeout_s) if timeout_s is not None else None
        last_id = since_event_id
        with open(self.journal_path) as f:
            while True:
                line = f.readline()
                if line:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("event_id", 0) > last_id:
                        last_id = ev["event_id"]
                        yield ev
                    continue
                if end_at is not None and time.time() >= end_at:
                    return
                time.sleep(poll_interval)
