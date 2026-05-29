"""subjob CLI — submit, status, follow, cancel.

JSON-by-default output so agents and other tooling can parse it. `--format
text` for humans.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from subjob.lib.pool import Pool
from subjob.lib.task import Task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subjob", description="Pilot-job scheduler for HPC.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sub = sub.add_parser("submit", help="Submit a task YAML to a pool")
    p_sub.add_argument("--pool", required=True)
    p_sub.add_argument("--task-file", required=True, help="Path to a task YAML")
    p_sub.set_defaults(func=cmd_submit)

    p_status = sub.add_parser("status", help="Show counts per state in a pool")
    p_status.add_argument("--pool", required=True)
    p_status.set_defaults(func=cmd_status)

    p_follow = sub.add_parser("follow", help="Tail the journal as JSONL")
    p_follow.add_argument("--pool", required=True)
    p_follow.add_argument("--since-event-id", type=int, default=0)
    p_follow.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Exit after this many seconds with no new events (default: never)",
    )
    p_follow.set_defaults(func=cmd_follow)

    p_cancel = sub.add_parser("cancel", help="Cancel a pending task")
    p_cancel.add_argument("--pool", required=True)
    p_cancel.add_argument("--task-id", required=True)
    p_cancel.set_defaults(func=cmd_cancel)

    p_reap = sub.add_parser(
        "reap-stale",
        help="Recover tasks stuck in claimed/ from a dead worker (manual; Phase 0 has no heartbeats)",
    )
    p_reap.add_argument("--pool", required=True)
    p_reap.add_argument(
        "--older-than",
        type=float,
        required=True,
        help="Reap claimed tasks whose file mtime is older than this many seconds",
    )
    p_reap.add_argument(
        "--to",
        choices=("pending", "failed"),
        default="pending",
        help="Where to move stale claims (default: pending, so a live worker retries them)",
    )
    p_reap.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be reaped without moving anything",
    )
    p_reap.set_defaults(func=cmd_reap_stale)

    p_failures = sub.add_parser("failures", help="Summarize failed tasks (read-only triage)")
    p_failures.add_argument("--pool", required=True)
    p_failures.add_argument(
        "--task-id",
        default=None,
        help="Inspect a single failed task in detail (includes command + longer stderr tail)",
    )
    p_failures.set_defaults(func=cmd_failures)

    args = parser.parse_args(argv)
    return args.func(args)


def cmd_submit(args) -> int:
    pool = Pool(args.pool)
    task = Task.read(Path(args.task_file))
    task_id = pool.submit(task)
    _emit(args.format, {"task_id": task_id, "pool": str(pool.root)})
    return 0


def cmd_status(args) -> int:
    pool = Pool(args.pool)
    s = pool.status()
    if args.format == "json":
        print(json.dumps(s))
    else:
        for k, v in s.items():
            print(f"{k}: {v}")
    return 0


def cmd_follow(args) -> int:
    pool = Pool(args.pool)
    try:
        for event in pool.follow(timeout_s=args.timeout, since_event_id=args.since_event_id):
            print(json.dumps(event), flush=True)
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_cancel(args) -> int:
    """Phase 0 cancel: remove a pending task (no-op for running tasks).

    Doesn't reach into a running worker. If the task is already claimed,
    we surface that fact and exit non-zero — Phase 1 will add real signal
    propagation.
    """
    pool = Pool(args.pool)
    name = f"{args.task_id}.yaml"
    pending = pool.pending_dir / name
    if pending.exists():
        # Move to failed/ with a cancelled marker so the history is intact.
        task = Task.read(pending)
        task.state = "failed"
        task.attempts = list(task.attempts) + [{"cancelled": True}]
        pending.write_text(task.to_yaml())
        os.rename(pending, pool.failed_dir / name)
        pool.emit("task_cancelled", args.task_id, {})
        _emit(args.format, {"cancelled": args.task_id, "was_pending": True})
        return 0
    claimed = pool.claimed_dir / name
    if claimed.exists():
        _emit(
            args.format,
            {
                "not_cancelled": args.task_id,
                "reason": "task is already claimed; running worker not interrupted",
            },
        )
        return 2
    _emit(args.format, {"error": "task not found in pool", "task_id": args.task_id})
    return 1


def cmd_reap_stale(args) -> int:
    """Move tasks stuck in claimed/ (dead worker) back to pending/ or failed/.

    Phase 0 has no heartbeats, so a worker whose node dies leaves its claims
    orphaned. This is the manual operator recovery path: anything in claimed/
    older than --older-than is reaped. Use a threshold safely larger than your
    longest task's walltime so you don't reap live work.
    """
    pool = Pool(args.pool)
    now = time.time()
    reaped = []
    for p in sorted(pool.claimed_dir.iterdir()) if pool.claimed_dir.exists() else []:
        if p.suffix != ".yaml":
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age < args.older_than:
            continue
        if args.dry_run:
            reaped.append({"task_id": p.stem, "age_s": round(age, 1), "action": "would-reap"})
            continue
        dest_dir = pool.pending_dir if args.to == "pending" else pool.failed_dir
        try:
            if args.to == "failed":
                task = Task.read(p)
                task.state = "failed"
                task.attempts = list(task.attempts) + [{"reaped_stale": True, "age_s": round(age, 1)}]
                p.write_text(task.to_yaml())
            os.rename(p, dest_dir / p.name)
            pool._pending_cache.pop(p.name, None)
            event = "task_released" if args.to == "pending" else "task_failed"
            pool.emit(event, p.stem, {"reaped_stale": True, "age_s": round(age, 1)})
            reaped.append({"task_id": p.stem, "age_s": round(age, 1), "moved_to": args.to})
        except OSError as e:
            reaped.append({"task_id": p.stem, "error": str(e)})
    _emit(args.format, {"reaped": reaped, "count": len(reaped), "dry_run": args.dry_run})
    return 0


def cmd_failures(args) -> int:
    """Read-only triage of failed tasks: exit code, walltime-kill, error, stderr tail."""
    pool = Pool(args.pool)
    if args.task_id:
        paths = [pool.failed_dir / f"{args.task_id}.yaml"]
    else:
        paths = pool.list_state("failed")
    items = []
    for p in paths:
        if not p.exists():
            continue
        t = Task.read(p)
        last = t.attempts[-1] if t.attempts else {}
        err_path = pool.logs_dir / f"{t.id}.err"
        tail_bytes = 8000 if args.task_id else 2000
        entry = {
            "task_id": t.id,
            "exit_code": last.get("exit_code"),
            "walltime_killed": last.get("walltime_killed"),
            "error": last.get("error"),
            "stderr_tail": _tail_bytes(err_path, tail_bytes),
        }
        if args.task_id:
            entry["command"] = t.command
        items.append(entry)
    _emit(args.format, {"failures": items, "count": len(items)})
    return 0


def _tail_bytes(path: Path, n: int) -> str:
    """Return the last n bytes of a file as text, or "" if absent/unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _emit(fmt: str, obj: dict) -> None:
    if fmt == "json":
        print(json.dumps(obj))
    else:
        for k, v in obj.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    sys.exit(main())
