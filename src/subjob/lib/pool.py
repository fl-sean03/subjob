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
import tempfile
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from subjob.lib.lock import atomic_move
from subjob.lib.task import Task

STATES = ("pending", "claimed", "done", "failed")


@dataclass
class ClaimedTask:
    """A task plus its current on-disk path (under claimed/)."""

    task: Task
    path: Path


class Pool:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.pending_dir = self.root / "pending"
        self.claimed_dir = self.root / "claimed"
        self.done_dir = self.root / "done"
        self.failed_dir = self.root / "failed"
        self.logs_dir = self.root / "logs"
        self.staging_dir = self.root / ".staging"
        self.journal_path = self.root / "journal.jsonl"

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
        ):
            d.mkdir(parents=True, exist_ok=True)
        self.journal_path.touch(exist_ok=True)

    # ----- submit -----

    def submit(self, task: Task) -> str:
        """Atomically place a single task in pending/. Returns the task id."""
        self.init()
        if self._task_exists_anywhere(task.id):
            raise ValueError(f"task id already in pool: {task.id!r}")
        text = task.to_yaml()
        # Write-then-rename so workers never see a partial YAML.
        tmp_path = self._stage_write(task.id, text)
        target = self.pending_dir / f"{task.id}.yaml"
        os.rename(tmp_path, target)
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

    def pending_paths(self) -> list[Path]:
        """Return pending task files, sorted by priority (descending), then by mtime.

        Unreadable / corrupt YAMLs are quarantined to failed/ so they don't
        poison-pill the polling loop forever.
        """
        items: list[tuple[int, float, Path]] = []
        for p in self.pending_dir.iterdir():
            if p.suffix != ".yaml":
                continue
            try:
                t = Task.read(p)
            except Exception as e:
                dest = self.failed_dir / p.name
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
            items.append((-t.priority, p.stat().st_mtime, p))
        items.sort()
        return [p for _, _, p in items]

    def claim(self, pending_path: Path) -> ClaimedTask | None:
        """Try to claim a pending task. Returns the loaded task or None on race loss."""
        target = self.claimed_dir / pending_path.name
        if not atomic_move(pending_path, target):
            return None
        try:
            task = Task.read(target)
        except Exception:
            # Corrupt YAML in claimed/ — bubble it out so the worker can decide.
            atomic_move(target, self.failed_dir / target.name)
            raise
        return ClaimedTask(task=task, path=target)

    def release(self, claimed: ClaimedTask) -> bool:
        """Move a claimed task back to pending. Used when a worker shuts down mid-claim."""
        dest = self.pending_dir / claimed.path.name
        if atomic_move(claimed.path, dest):
            self.emit("task_released", claimed.task.id, {})
            return True
        return False

    def commit_done(self, claimed: ClaimedTask, payload: dict[str, Any]) -> None:
        self._finalize(claimed, "done", "task_done", payload)

    def commit_failed(self, claimed: ClaimedTask, payload: dict[str, Any]) -> None:
        self._finalize(claimed, "failed", "task_failed", payload)

    def _finalize(self, claimed: ClaimedTask, state: str, event_type: str, payload: dict) -> None:
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
            "event_id": time.time_ns(),
            "type": event_type,
            "task_id": task_id,
            "payload": payload,
        }
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.journal_path, "a") as f:
            f.write(json.dumps(event) + "\n")
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
                ev = json.loads(line)
                if ev["event_id"] > since_event_id:
                    out.append(ev)
        return out

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
                    ev = json.loads(line)
                    if ev["event_id"] > last_id:
                        last_id = ev["event_id"]
                        yield ev
                    continue
                if end_at is not None and time.time() >= end_at:
                    return
                time.sleep(poll_interval)
