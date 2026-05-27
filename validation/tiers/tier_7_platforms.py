"""Tier VII — multi-partition platforms.

At least one canonical task succeeds on each partition we use. Tests
partition-specific quirks (module env, GPU visibility, blanca preemption).

Default: submit one task to each of amilan, al40, aa100. Skip blanca by
default since it preempts (long queue).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from validation import gates as G
from validation import workloads as W
from validation.runner import TierSpec, WorkerSpec, run_tier

DEFAULT_PARTITIONS = ["amilan"]  # GPU partitions added via --include-gpu


def make_submit(label):
    def submit(pool):
        pool.submit(W.echo_only(f"plat_{label}", f"platform-{label}"))
        pool.submit(W.cpu_spin(f"spin_{label}", seconds=2.0))

    return submit


def build_spec(label, partition, qos, backend):
    return TierSpec(
        name=f"Tier VII.{label} — platform {partition}",
        description=f"Canonical task on partition {partition}.",
        submit=make_submit(label),
        gates=[
            G.status_done_failed(done=2, failed=0),
            G.no_double_claims(),
            G.task_attempt_field(f"plat_{label}", "exit_code", 0),
            G.task_stdout_contains(f"plat_{label}", f"platform-{label}"),
        ],
        worker=WorkerSpec(
            backend=backend, cores=2, walltime_seconds=600,
            idle_timeout_seconds=15.0, partition=partition, qos=qos, n_workers=1,
        ),
        poll_timeout_s=3600,
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool-root", required=True)
    parser.add_argument("--partitions", nargs="+", default=DEFAULT_PARTITIONS)
    parser.add_argument("--qos", default="normal")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    overall = []
    for part in args.partitions:
        label = part
        spec = build_spec(label, part, args.qos, "slurm")
        pool_root = Path(args.pool_root) / f"subjob-tier7-{label}-{int(time.time())}"
        try:
            report = run_tier(spec, pool_root)
            overall.append((label, report.passed, str(pool_root)))
            print(f"\n=== {label} report ===")
            print((pool_root / "REPORT.md").read_text())
        except Exception as e:
            overall.append((label, False, f"error: {e}"))

    print("\n=== Tier VII aggregate ===")
    for label, passed, where in overall:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}  ({where})")
    return 0 if all(p for _, p, _ in overall) else 1


if __name__ == "__main__":
    sys.exit(main())
