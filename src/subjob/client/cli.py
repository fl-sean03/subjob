"""subjob CLI — submit, status, follow, cancel.

JSON-by-default output so agents and other tooling can parse it. `--format
text` for humans.
"""

from __future__ import annotations

import argparse
import json
import sys
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
        import os

        os.rename(pending, pool.failed_dir / name)
        pool.emit("task_cancelled", args.task_id, {})
        _emit(args.format, {"cancelled": args.task_id, "was_pending": True})
        return 0
    claimed = pool.claimed_dir / name
    if claimed.exists():
        _emit(args.format, {"cancelled": args.task_id, "was_pending": False, "note": "task is already claimed; running worker not interrupted"})
        return 2
    _emit(args.format, {"error": "task not found in pool", "task_id": args.task_id})
    return 1


def _emit(fmt: str, obj: dict) -> None:
    if fmt == "json":
        print(json.dumps(obj))
    else:
        for k, v in obj.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    sys.exit(main())
