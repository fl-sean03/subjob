"""Tier V — recovery + chaos.

Tests the worker's behavior under hostile conditions. Run as a sequence
of scenarios; each writes its own pool + report.

Scenarios:
  E.1 mid-task-scancel — claim a long task, scancel worker, run a new
                          worker, expect task to complete.
  E.2 corrupt-yaml     — submit task, truncate file, submit a new task,
                          expect worker to handle gracefully + finish ok.
  E.3 huge-pool        — submit 5000 trivial tasks, 1 worker drains them.
                          Verify pool.pending_paths() doesn't OOM and
                          worker drains within reasonable time.

Phase-0 doesn't have heartbeats / dead-worker detection — E.1 + E.4
exercise what's there.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from subjob.lib.pool import Pool
from subjob.lib.task import Resources, Task
from validation import gates as G
from validation import workloads as W
from validation.report import TierReport
from validation.runner import TierSpec, WorkerSpec, run_tier

# ---------- E.3 huge pool ----------


def build_huge_pool_spec(backend, partition, qos):
    n = 5000

    def submit(pool):
        for i in range(n):
            pool.submit(W.echo_only(f"t{i:05d}", f"x{i}"))

    return TierSpec(
        name=f"Tier V.E3 — huge pool ({n} tasks)",
        description="Test pool.pending_paths() scaling + journal at 5k tasks.",
        submit=submit,
        gates=[
            G.no_double_claims(),
            G.status_done_failed(done=n, failed=0),
            G.journal_event_count("task_submitted", n),
            G.journal_event_count("task_done", n),
            # Floor reflects single-worker GPFS reality: each task is ~6
            # metadata ops (claim rename + commit write/rename + 3 journal
            # appends). Measured 4.27 tasks/s for 1×8-core worker after the
            # F-001 cache fix (was 2.38 before). Cluster-wide throughput
            # scales with worker count (13.1/s at 8 workers — see Tier IV.d).
            G.throughput_at_least(3.0),
        ],
        worker=WorkerSpec(
            backend=backend, cores=8, walltime_seconds=1800,
            idle_timeout_seconds=30.0, partition=partition, qos=qos, n_workers=1,
        ),
        poll_timeout_s=2700,
        expected_terminal_tasks=n,
    )


# ---------- E.1 mid-task scancel ----------


def run_mid_task_scancel(pool_root: Path):
    """Submit a long task; spawn worker A; scancel worker A mid-task; spawn
    worker B; verify task lands in done/."""
    pool = Pool(pool_root)
    pool.init()
    pool.submit(Task(
        id="long_task",
        command="echo running; sleep 20; echo finished",
        resources=Resources(cores=1, walltime_seconds=60),
    ))

    # Worker A (subprocess) starts the task, runs for 5s, then we kill it
    log = logging.getLogger("tier5.e1")
    import subprocess

    sys_exe = sys.executable
    log.info("starting worker A...")
    proc_a = subprocess.Popen(
        [sys_exe, "-m", "subjob.worker",
         "--pool", str(pool_root),
         "--cores", "1",
         "--poll-interval", "0.2",
         "--idle-timeout", "60",
         "--log-level", "WARNING"],
    )
    time.sleep(5)  # let worker claim + start the task
    log.info("status mid-task: %s", pool.status())
    log.info("killing worker A (SIGKILL — simulating node death)")
    proc_a.kill()
    proc_a.wait()

    # Manually release the orphaned claim (Phase 0 has no heartbeats)
    # In real Phase 1 this would be a periodic stale-claim sweep.
    log.info("manually releasing orphaned claim...")
    for p in (pool_root / "claimed").iterdir():
        os.rename(p, pool_root / "pending" / p.name)
        pool.emit("task_released", p.stem, {"reason": "manual_release_post_dead_worker"})

    # Worker B picks it up
    log.info("starting worker B...")
    proc_b = subprocess.Popen(
        [sys_exe, "-m", "subjob.worker",
         "--pool", str(pool_root),
         "--cores", "1",
         "--poll-interval", "0.2",
         "--idle-timeout", "5",
         "--log-level", "WARNING"],
    )
    proc_b.wait(timeout=60)

    return pool


def build_e1_gates():
    return [
        G.status_done_failed(done=1, failed=0),
        # Should see at least 2 task_claimed events: original + post-release
        # (worker_started events: 2)
        G.journal_event_count("worker_started", 2),
        G.journal_event_present("task_released", "long_task"),
        G.journal_event_present("task_done", "long_task"),
        G.task_attempt_field("long_task", "exit_code", 0),
        G.task_stdout_contains("long_task", "finished"),
    ]


# ---------- E.2 corrupt yaml ----------


def run_corrupt_yaml(pool_root: Path):
    pool = Pool(pool_root)
    pool.init()
    pool.submit(W.echo_only("ok1", "alpha"))
    pool.submit(W.echo_only("ok2", "beta"))
    # Submit then corrupt the YAML
    pool.submit(W.echo_only("corrupt", "should not run"))
    corrupt_path = pool_root / "pending" / "corrupt.yaml"
    corrupt_path.write_text("!!! NOT VALID YAML !!!\n garbage: [unclosed\n")

    import subprocess
    sys_exe = sys.executable
    proc = subprocess.Popen(
        [sys_exe, "-m", "subjob.worker",
         "--pool", str(pool_root),
         "--cores", "1",
         "--poll-interval", "0.2",
         "--idle-timeout", "5",
         "--log-level", "WARNING"],
    )
    proc.wait(timeout=30)
    return pool


def build_e2_gates():
    return [
        # Two ok tasks complete; the corrupt one goes to failed/
        G.all_in_state("done", {"ok1", "ok2"}),
        # Corrupt YAML gets quarantined to failed/
        G.all_in_state("failed", {"corrupt"}),
        # Worker survived and continued past the corruption
        G.task_stdout_contains("ok2", "beta"),
    ]


# ---------- entrypoint ----------


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=("e1", "e2", "e3"))
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--pool-root", default=None)
    parser.add_argument("--partition", default="amilan")
    parser.add_argument("--qos", default="normal")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    pool_root = (
        Path(args.pool_root) / f"subjob-tier5{args.scenario}-{int(time.time())}"
        if args.pool_root
        else Path("/tmp") / f"subjob-tier5{args.scenario}-{int(time.time())}"
    )
    pool_root.mkdir(parents=True, exist_ok=True)

    if args.scenario == "e1":
        pool = run_mid_task_scancel(pool_root)
        gates = build_e1_gates()
        name = "Tier V.E1 — mid-task scancel + recovery"
    elif args.scenario == "e2":
        pool = run_corrupt_yaml(pool_root)
        gates = build_e2_gates()
        name = "Tier V.E2 — corrupt YAML quarantine"
    else:  # e3
        spec = build_huge_pool_spec("local" if args.local else "slurm", args.partition, args.qos)
        report = run_tier(spec, pool_root)
        print()
        print((pool_root / "REPORT.md").read_text())
        return 0 if report.passed else 1

    report = TierReport(
        tier_name=name,
        pool_path=str(pool_root),
        backend="local",
        worker_summary="custom orchestration (see scenario code)",
        duration_s=0.0,
    )
    for g in gates:
        report.gates.append(g(pool))
    report.write(pool_root / "REPORT.md")
    print((pool_root / "REPORT.md").read_text())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
