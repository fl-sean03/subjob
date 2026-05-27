"""Tier IV — concurrency stress (iterative).

Four sub-tiers, each escalating one variable. Each must pass before the
next is attempted.

  IV.a — 50 tasks  ×  2 workers ×  1 core   (Phase-0-Stage-4 baseline)
  IV.b — 200 tasks ×  4 workers ×  4 cores  (4× scale + 4-way internal concurrency)
  IV.c — 500 tasks ×  4 workers ×  4 cores  (2.5× more tasks at same worker count)
  IV.d — 1000 tasks × 8 workers ×  8 cores  (production-like)

Run:
    python -m validation.tiers.tier_4_concurrency a|b|c|d --pool-root /scratch/...
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

PROFILES = {
    "a": dict(tasks=50, workers=2, cores=1, label="IV.a 50 × 2 × 1"),
    "b": dict(tasks=200, workers=4, cores=4, label="IV.b 200 × 4 × 4"),
    "c": dict(tasks=500, workers=4, cores=4, label="IV.c 500 × 4 × 4"),
    "d": dict(tasks=1000, workers=8, cores=8, label="IV.d 1000 × 8 × 8"),
}


def make_submit(n_tasks):
    def submit(pool):
        for i in range(n_tasks):
            # Mix sleep and echo so concurrent dispatch surfaces in the journal
            if i % 2 == 0:
                pool.submit(W.sleep_random(f"t{i:04d}", lo=0.5, hi=1.5, seed=i))
            else:
                pool.submit(W.echo_only(f"t{i:04d}", f"task-{i}"))

    return submit


def build_gates(n_tasks, expected_min_concurrent):
    return [
        G.no_double_claims(),
        G.status_done_failed(done=n_tasks, failed=0),
        G.journal_event_count("task_submitted", n_tasks),
        G.journal_event_count("task_claimed", n_tasks),
        G.journal_event_count("task_done", n_tasks),
        G.concurrent_at_peak(expected_min_concurrent),
        G.throughput_at_least(1.0),  # at least 1 task/s overall
    ]


def build_spec(letter, backend, partition, qos):
    p = PROFILES[letter]
    expected_concurrency = max(p["workers"] * p["cores"] // 2, 2)
    return TierSpec(
        name=f"Tier IV.{letter} — {p['label']}",
        description=f"{p['tasks']} tasks across {p['workers']} workers × {p['cores']} cores.",
        submit=make_submit(p["tasks"]),
        gates=build_gates(p["tasks"], expected_concurrency),
        worker=WorkerSpec(
            backend=backend,
            cores=p["cores"],
            walltime_seconds=1800,
            idle_timeout_seconds=20.0,
            partition=partition,
            qos=qos,
            n_workers=p["workers"],
        ),
        poll_timeout_s=2400,
        expected_terminal_tasks=p["tasks"],
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("sub", choices=list(PROFILES))
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--pool-root", default=None)
    parser.add_argument("--partition", default="amilan")
    parser.add_argument("--qos", default="normal")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    spec = build_spec(args.sub, "local" if args.local else "slurm", args.partition, args.qos)

    if args.pool_root:
        pool_root = Path(args.pool_root) / f"subjob-tier4{args.sub}-{int(time.time())}"
    else:
        pool_root = Path(tempfile.mkdtemp(prefix=f"subjob-tier4{args.sub}-"))

    report = run_tier(spec, pool_root)
    print()
    print((pool_root / "REPORT.md").read_text())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
