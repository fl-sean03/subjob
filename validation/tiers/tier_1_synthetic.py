"""Tier I — synthetic compute primitives (lab-agnostic, Cat A).

Goal: prove the worker handles arbitrary commands, not just echo/sleep.

Default backend is 'slurm' (1 amilan worker). Pass --local for in-process
execution against a /tmp pool (cheap sanity).

Workload mix:
  cpu_spin × 3 (1s, 3s, 8s)
  mem_alloc × 2 (0.5 GB, 2 GB)
  disk_io × 2 (10 MB, 200 MB)
  stdout_firehose × 1
  signal_grace × 1 (catches SIGTERM cleanly)
  signal_ignore × 1 (forces SIGKILL fallback)
  exit_code matrix: 0, 1, 7, 42
  echo_only × 2 (throughput floor)

Total: 14 tasks.

Run with:
    python -m validation.tiers.tier_1_synthetic [--local] [--pool-root <dir>]
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from pathlib import Path

from validation import gates as G
from validation import workloads as W
from validation.runner import TierSpec, WorkerSpec, run_tier

PASS_IDS = {
    "cpu_spin_1s", "cpu_spin_3s", "cpu_spin_8s",
    "mem_500mb", "mem_2gb",
    "disk_10mb", "disk_200mb",
    "stdout_10k",
    "exit_0",
    "echo_a", "echo_b",
}

# Both signal tasks land in failed/ — walltime contract violation is a
# failure regardless of how the child responded. The difference: for
# signal_grace, stdout must contain "caught SIGTERM" (proves the SIGTERM
# handler ran before the runner had to escalate). For signal_ignore the
# child ignored SIGTERM and was SIGKILLed.
FAIL_IDS = {
    "signal_grace",
    "signal_ignore",
    "exit_1", "exit_7", "exit_42",
}


def submit(pool):
    pool.submit(W.cpu_spin("cpu_spin_1s", seconds=1.0))
    pool.submit(W.cpu_spin("cpu_spin_3s", seconds=3.0))
    pool.submit(W.cpu_spin("cpu_spin_8s", seconds=8.0))
    pool.submit(W.mem_alloc("mem_500mb", gb=0.5, hold_s=0.5))
    pool.submit(W.mem_alloc("mem_2gb", gb=2.0, hold_s=0.5))
    pool.submit(W.disk_io("disk_10mb", mb=10))
    pool.submit(W.disk_io("disk_200mb", mb=200))
    pool.submit(W.stdout_firehose("stdout_10k", lines=10000))
    pool.submit(W.signal_grace("signal_grace"))
    pool.submit(W.signal_ignore("signal_ignore"))
    for rc in (0, 1, 7, 42):
        pool.submit(W.exit_code(f"exit_{rc}", rc))
    pool.submit(W.echo_only("echo_a", "tier1-a"))
    pool.submit(W.echo_only("echo_b", "tier1-b"))


def build_gates() -> list[G.Gate]:
    return [
        # Final placement
        G.status_done_failed(done=len(PASS_IDS), failed=len(FAIL_IDS)),
        G.all_in_state("done", PASS_IDS),
        G.all_in_state("failed", FAIL_IDS),

        # Claim invariant
        G.no_double_claims(),
        G.journal_event_count("task_submitted", len(PASS_IDS) + len(FAIL_IDS)),
        G.journal_event_count("task_claimed", len(PASS_IDS) + len(FAIL_IDS)),

        # Exit code propagation
        G.task_attempt_field("exit_0", "exit_code", 0),
        G.task_attempt_field("exit_1", "exit_code", 1),
        G.task_attempt_field("exit_7", "exit_code", 7),
        G.task_attempt_field("exit_42", "exit_code", 42),

        # Walltime enforcement: both signal tasks are walltime-killed.
        G.task_attempt_field("signal_grace", "walltime_killed", True),
        G.task_attempt_field("signal_ignore", "walltime_killed", True),
        # But the SIGTERM handler in signal_grace MUST have run before death:
        G.task_stdout_contains("signal_grace", "caught SIGTERM"),

        # Durations
        G.task_duration_within("cpu_spin_1s", 0.5, 4.0),
        G.task_duration_within("cpu_spin_3s", 2.0, 8.0),
        G.task_duration_within("cpu_spin_8s", 6.0, 16.0),
        G.task_duration_within("signal_ignore", 1.5, 12.0),  # walltime + SIGKILL grace

        # Stdout capture
        G.task_stdout_contains("echo_a", "tier1-a"),
        G.task_stdout_contains("cpu_spin_1s", "cpu_spin done"),
        G.task_stdout_contains("disk_10mb", "disk_io done 10 MB"),
        # Firehose: only the last ~4 KB is preserved; we just check SOMETHING is there
        G.task_stdout_contains("stdout_10k", "subjob stdout firehose line"),

        # Throughput floor (worker did SOMETHING per second on average)
        G.throughput_at_least(0.1),
    ]


def build_spec(backend: str, partition: str | None, qos: str | None, cores: int) -> TierSpec:
    worker = WorkerSpec(
        backend=backend,
        cores=cores,
        walltime_seconds=600,
        idle_timeout_seconds=15.0,
        partition=partition,
        qos=qos,
        n_workers=1,
    )
    return TierSpec(
        name="Tier I — synthetic compute primitives",
        description="Cat A workloads (cpu/mem/disk/stdout/signal/exit-code) on one worker.",
        submit=submit,
        gates=build_gates(),
        worker=worker,
        poll_timeout_s=1200,
        expected_terminal_tasks=len(PASS_IDS) + len(FAIL_IDS),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="run in-process instead of sbatch")
    parser.add_argument("--pool-root", default=None, help="pool root dir (default: tmpdir)")
    parser.add_argument("--partition", default="amilan")
    parser.add_argument("--qos", default="normal")
    parser.add_argument("--cores", type=int, default=4)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    backend = "local" if args.local else "slurm"
    spec = build_spec(backend, args.partition, args.qos, args.cores)

    if args.pool_root:
        pool_root = Path(args.pool_root) / f"subjob-tier1-{int(time.time())}"
    else:
        tmp = tempfile.mkdtemp(prefix="subjob-tier1-")
        pool_root = Path(tmp)

    report = run_tier(spec, pool_root)
    print()
    print((pool_root / "REPORT.md").read_text())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
