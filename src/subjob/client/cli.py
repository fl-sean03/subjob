"""subjob CLI — submit, status, follow, cancel.

JSON-by-default output so agents and other tooling can parse it. `--format
text` for humans.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import time
from pathlib import Path

from subjob.lib.pool import Pool
from subjob.lib.task import Task

# Errors a bad/missing/invalid task file (or duplicate id) can raise. We turn
# these into structured JSON instead of a raw traceback. ParseError subclasses
# ValueError, but list it explicitly so the intent survives any future refactor.
_SUBMIT_ERRORS: tuple[type[Exception], ...] = (FileNotFoundError, OSError, ValueError)
try:
    from subjob.lib.yaml_lite import ParseError as _ParseError

    _SUBMIT_ERRORS = (*_SUBMIT_ERRORS, _ParseError)
except ImportError:  # pragma: no cover - yaml_lite always importable in practice
    pass


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
        help="Recover tasks stuck in claimed/ from a dead worker (mtime-based; --auto uses heartbeats)",
    )
    p_reap.add_argument("--pool", required=True)
    p_reap.add_argument(
        "--older-than",
        type=float,
        default=None,
        help=(
            "Mtime threshold in seconds (default mode). With --auto, instead "
            "the heartbeat-staleness threshold (default 120s = 4× the worker "
            "heartbeat interval)."
        ),
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
    p_reap.add_argument(
        "--auto",
        action="store_true",
        help=(
            "Use heartbeat-based liveness: reap claims whose owner worker has "
            "no heartbeat file or a heartbeat older than --older-than. Falls "
            "back to mtime for legacy claims with no owner stamp."
        ),
    )
    p_reap.set_defaults(func=cmd_reap_stale)

    p_archive = sub.add_parser(
        "archive",
        help="Gzip-archive old terminal task YAMLs out of done/ (and optionally failed/)",
    )
    p_archive.add_argument("--pool", required=True)
    p_archive.add_argument(
        "--older-than-days",
        type=float,
        required=True,
        help="Archive task YAMLs whose file mtime is older than this many days",
    )
    p_archive.add_argument(
        "--include-failed",
        action="store_true",
        help="Also archive failed/ tasks (default: done/ only)",
    )
    p_archive.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be archived without moving anything",
    )
    p_archive.set_defaults(func=cmd_archive)

    p_failures = sub.add_parser("failures", help="Summarize failed tasks (read-only triage)")
    p_failures.add_argument("--pool", required=True)
    p_failures.add_argument(
        "--task-id",
        default=None,
        help="Inspect a single failed task in detail (includes command + longer stderr tail)",
    )
    p_failures.set_defaults(func=cmd_failures)

    p_diag = sub.add_parser(
        "diagnose",
        help=(
            "Classify failed tasks against the pool's priors.yaml catalog "
            "(advisory; complements `failures`)"
        ),
    )
    p_diag.add_argument("--pool", required=True)
    p_diag.add_argument(
        "--task-id",
        default=None,
        help=(
            "Diagnose a single failed task; omit to diagnose every failed "
            "task in the pool."
        ),
    )
    p_diag.set_defaults(func=cmd_diagnose)

    args = parser.parse_args(argv)
    return args.func(args)


def cmd_submit(args) -> int:
    pool = Pool(args.pool)
    try:
        task = Task.read(Path(args.task_file))
        task_id = pool.submit(task)
    except _SUBMIT_ERRORS as e:
        # Bad/missing/invalid task file (or duplicate id) → emit structured
        # JSON, not a raw traceback an agent can't parse.
        _emit(args.format, {"error": str(e), "task_file": args.task_file})
        return 1
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
        try:
            task = Task.read(pending)
        except (OSError, ValueError) as e:
            _emit(args.format, {"error": str(e), "task_id": args.task_id})
            return 1
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

    Two modes:

    - Default (mtime): anything in claimed/ older than --older-than is reaped.
      Use a threshold safely larger than your longest task's walltime so you
      don't reap live work.
    - --auto: heartbeat-based. For each claim, look up the owner worker's
      heartbeat file: missing → reap (owner_missing); older than --older-than
      → reap (owner_stale_*s). Legacy claims with no owner stamp fall back to
      mtime (legacy_mtime_*s). Default --older-than for --auto is 120s
      (4× the worker heartbeat interval).
    """
    pool = Pool(args.pool)
    now = time.time()
    # Default threshold differs between modes.
    if args.older_than is None:
        threshold = 120.0 if args.auto else None
        if threshold is None:
            _emit(
                args.format,
                {"error": "--older-than is required (no default in mtime mode)"},
            )
            return 1
    else:
        threshold = args.older_than

    reaped: list[dict] = []
    if not pool.claimed_dir.exists():
        _emit(args.format, {"reaped": reaped, "count": 0, "dry_run": args.dry_run})
        return 0

    if args.auto:
        # Heartbeat-driven path. Preview mode classifies but doesn't move.
        for p in sorted(pool.claimed_dir.iterdir()):
            if p.suffix != ".yaml":
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            try:
                task = Task.read(p)
            except (OSError, ValueError) as e:
                reaped.append({"task_id": p.stem, "error": f"unreadable: {e}"})
                continue
            stamp = Pool._last_claim_stamp(task)
            reason: str | None = None
            if stamp is not None:
                owner = stamp.get("claimed_by")
                hb_ns = pool.read_heartbeat_ns(owner) if owner else None
                if hb_ns is None:
                    reason = "owner_missing"
                else:
                    age_s = now - (hb_ns / 1e9)
                    if age_s > threshold:
                        reason = f"owner_stale_{int(age_s)}s"
            else:
                age_s = now - mtime
                if age_s > threshold:
                    reason = f"legacy_mtime_{int(age_s)}s"
            if reason is None:
                continue
            if args.dry_run:
                reaped.append(
                    {"task_id": p.stem, "reason": reason, "action": "would-reap"}
                )
                continue
            dest_dir = pool.pending_dir if args.to == "pending" else pool.failed_dir
            try:
                payload: dict = {"reaped_stale": True, "reason": reason}
                if args.to == "failed":
                    task.state = "failed"
                task.attempts = list(task.attempts) + [payload]
                p.write_text(task.to_yaml())
                os.rename(p, dest_dir / p.name)
                pool._pending_cache.pop(p.name, None)
                event = "task_released" if args.to == "pending" else "task_failed"
                pool.emit(event, p.stem, payload)
                reaped.append(
                    {"task_id": p.stem, "reason": reason, "moved_to": args.to}
                )
            except (OSError, ValueError) as e:
                reaped.append({"task_id": p.stem, "error": str(e)})
        _emit(args.format, {"reaped": reaped, "count": len(reaped), "dry_run": args.dry_run})
        return 0

    # Legacy mtime path — unchanged behavior.
    for p in sorted(pool.claimed_dir.iterdir()):
        if p.suffix != ".yaml":
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age < threshold:
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
        except (OSError, ValueError) as e:
            reaped.append({"task_id": p.stem, "error": str(e)})
    _emit(args.format, {"reaped": reaped, "count": len(reaped), "dry_run": args.dry_run})
    return 0


def cmd_archive(args) -> int:
    """Gzip-archive old terminal task YAMLs to keep the pool from growing forever.

    Moves task YAMLs in done/ (and failed/ with --include-failed) whose file
    mtime is older than --older-than-days into <pool>/archive/ as <id>.yaml.gz.
    The journal is deliberately left untouched — readers tail it, so journal
    rotation stays a documented manual step (see DEPLOYMENT.md §4).
    """
    pool = Pool(args.pool)
    archive_dir = pool.root / "archive"
    cutoff = time.time() - args.older_than_days * 86400
    src_dirs = [pool.done_dir]
    if args.include_failed:
        src_dirs.append(pool.failed_dir)
    archived: list[str] = []
    if not args.dry_run:
        archive_dir.mkdir(parents=True, exist_ok=True)
    for d in src_dirs:
        if not d.exists():
            continue
        for p in sorted(d.iterdir()):
            if p.suffix != ".yaml":
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            if args.dry_run:
                archived.append(p.stem)
                continue
            dest = archive_dir / f"{p.name}.gz"
            try:
                with open(p, "rb") as src, gzip.open(dest, "wb") as gz:
                    shutil.copyfileobj(src, gz)
                os.unlink(p)
            except OSError:
                # Clean up a half-written archive so a re-run can retry.
                try:
                    if dest.exists():
                        os.unlink(dest)
                except OSError:
                    pass
                continue
            archived.append(p.stem)
    _emit(
        args.format,
        {"archived": archived, "count": len(archived), "dry_run": args.dry_run},
    )
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
        try:
            t = Task.read(p)
        except (OSError, ValueError) as e:
            # A corrupt failed/ YAML shouldn't crash the whole triage listing.
            items.append({"task_id": p.stem, "error": f"unreadable: {e}"})
            continue
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
        # Surface artifact-validation failures in triage so the operator sees
        # "command exited 0 but didn't produce $SNAP_DIR/run.log" rather than
        # a confusing exit_code: 0 with no obvious cause.
        if last.get("artifact_validation_failed"):
            entry["artifact_validation_failed"] = True
            entry["artifact_detail"] = last.get("artifact_detail")
        if args.task_id:
            entry["command"] = t.command
        items.append(entry)
    _emit(args.format, {"failures": items, "count": len(items)})
    return 0


def cmd_diagnose(args) -> int:
    """Run the priors catalog over failed task(s); print verdict + matches.

    Two modes:
      - ``--task-id``: diagnose one task; print the full verdict dict.
      - omitted: diagnose every failed task; print a list of one-line
        verdicts plus a count.

    Missing priors.yaml is NOT an error — the verdict is just "unknown"
    with no matches. A MALFORMED priors.yaml surfaces as an `error` field
    on the diagnose result (pool.diagnose() no longer raises); we promote
    that to exit 1 so the operator sees a non-zero status.
    """
    pool = Pool(args.pool)
    if args.task_id:
        result = pool.diagnose(args.task_id)
        _emit(args.format, result)
        # Only "priors.yaml unreadable" errors warrant non-zero exit; an
        # "task not in failed/" error is a legitimate lookup result.
        if "error" in result and "priors.yaml" in result["error"]:
            return 1
        return 0
    # Batch mode: one-line entry per failed task.
    items = []
    schema_error: str | None = None
    for p in pool.list_state("failed"):
        r = pool.diagnose(p.stem)
        if "error" in r and "priors.yaml" in r["error"]:
            # All subsequent diagnoses will hit the same broken priors.yaml;
            # surface once and stop walking the failed list.
            schema_error = r["error"]
            break
        fix = r.get("suggested_fix") or ""
        # Collapse to a single line for the batch summary.
        fix_line = fix.strip().splitlines()[0] if fix.strip() else ""
        items.append(
            {
                "task_id": r["task_id"],
                "verdict": r["verdict"],
                "suggested_fix": fix_line,
            }
        )
    if schema_error is not None:
        _emit(args.format, {"error": schema_error})
        return 1
    _emit(args.format, {"diagnoses": items, "count": len(items)})
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
