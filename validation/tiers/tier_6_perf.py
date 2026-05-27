"""Tier VI — performance baseline.

No pass/fail; record numbers for future regression detection.

Workload: 200 trivial echo tasks, 1 worker × 8 cores. Measures:
  - sustained throughput (tasks/s)
  - per-task claim latency (submitted → claimed)
  - per-task journal write rate
  - submit batch throughput (1000 tasks via Pool.submit_batch in process)
  - pool.pending_paths() time at N=10 000

Baseline is written to validation/baselines/<date>.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import tempfile
import time
from pathlib import Path

from subjob.lib.pool import Pool
from validation import workloads as W
from validation.runner import TierSpec, WorkerSpec, run_tier


def _percentile(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = int(round((p / 100) * (len(xs) - 1)))
    return xs[k]


def measure_throughput(pool_root: Path, n_tasks: int = 200, backend: str = "local",
                       partition: str = "amilan", qos: str = "normal") -> dict:
    def submit(pool):
        for i in range(n_tasks):
            pool.submit(W.echo_only(f"perf{i:04d}"))

    spec = TierSpec(
        name=f"Tier VI — throughput baseline ({n_tasks} tasks)",
        description="Measure subjob throughput on trivial workload.",
        submit=submit,
        gates=[],
        worker=WorkerSpec(
            backend=backend, cores=8, walltime_seconds=600,
            idle_timeout_seconds=20.0, partition=partition, qos=qos, n_workers=1,
        ),
        poll_timeout_s=900,
        expected_terminal_tasks=n_tasks,
    )
    run_tier(spec, pool_root)

    pool = Pool(pool_root)
    events = pool.read_journal()

    submitted = {e["task_id"]: e["event_id"] for e in events if e["type"] == "task_submitted"}
    claimed = {e["task_id"]: e["event_id"] for e in events if e["type"] == "task_claimed"}
    done = {e["task_id"]: e["event_id"] for e in events if e["type"] == "task_done"}
    worker_starts = [e["event_id"] for e in events if e["type"] == "worker_started"]
    worker_stops = [e["event_id"] for e in events if e["type"] == "worker_stopped"]

    # Claim latency: submitted → claimed (ns to s)
    claim_lat_s = []
    for tid, sid in submitted.items():
        if tid in claimed:
            claim_lat_s.append((claimed[tid] - sid) / 1e9)

    # Worker-side throughput
    if worker_starts and done:
        span_s = (max(done.values()) - worker_starts[0]) / 1e9
        throughput = len(done) / span_s if span_s > 0 else 0
    else:
        throughput = 0

    return {
        "n_tasks": n_tasks,
        "throughput_tasks_per_s": round(throughput, 2),
        "claim_lat_p50_s": round(_percentile(claim_lat_s, 50) or 0, 4),
        "claim_lat_p99_s": round(_percentile(claim_lat_s, 99) or 0, 4),
        "claim_lat_mean_s": round(statistics.mean(claim_lat_s) if claim_lat_s else 0, 4),
        "worker_starts": len(worker_starts),
        "worker_stops": len(worker_stops),
        "tasks_done": len(done),
    }


def measure_submit_rate(n: int = 1000) -> dict:
    with tempfile.TemporaryDirectory(prefix="subjob-perf-submit-") as tmp:
        pool = Pool(Path(tmp))
        pool.init()
        tasks = [W.echo_only(f"s{i:05d}") for i in range(n)]
        t = time.time()
        for task in tasks:
            pool.submit(task)
        elapsed = time.time() - t
        return {
            "n_tasks_submitted": n,
            "submit_elapsed_s": round(elapsed, 3),
            "submit_rate_tasks_per_s": round(n / elapsed, 1),
        }


def measure_pending_listing(n: int = 10000) -> dict:
    with tempfile.TemporaryDirectory(prefix="subjob-perf-list-") as tmp:
        pool = Pool(Path(tmp))
        pool.init()
        for i in range(n):
            pool.submit(W.echo_only(f"x{i:05d}"))
        # Measure pending_paths
        samples = []
        for _ in range(5):
            t = time.time()
            paths = pool.pending_paths()
            samples.append(time.time() - t)
        return {
            "n_pending_in_pool": n,
            "pending_paths_listing_s_mean": round(statistics.mean(samples), 3),
            "pending_paths_listing_s_min": round(min(samples), 3),
            "pending_paths_listing_s_max": round(max(samples), 3),
            "tasks_returned": len(paths),
        }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--pool-root", default=None)
    parser.add_argument("--partition", default="amilan")
    parser.add_argument("--qos", default="normal")
    parser.add_argument(
        "--baseline-dir",
        default=str(Path(__file__).resolve().parent.parent / "baselines"),
        help="Directory to write baseline markdown (default: validation/baselines)",
    )
    parser.add_argument("--n-throughput-tasks", type=int, default=200)
    parser.add_argument(
        "--skip-listing", action="store_true",
        help="Skip the 10k pending_paths timing (it takes time + disk)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    backend = "local" if args.local else "slurm"

    if args.pool_root:
        pool_root = Path(args.pool_root) / f"subjob-tier6-{int(time.time())}"
    else:
        pool_root = Path(tempfile.mkdtemp(prefix="subjob-tier6-"))

    print("=== throughput (worker-side) ===")
    throughput = measure_throughput(pool_root, args.n_throughput_tasks, backend, args.partition, args.qos)
    print(json.dumps(throughput, indent=2))

    print("=== submit rate (in-process) ===")
    submit = measure_submit_rate(1000)
    print(json.dumps(submit, indent=2))

    if not args.skip_listing:
        print("=== pool.pending_paths() at N=10000 ===")
        listing = measure_pending_listing(10000)
        print(json.dumps(listing, indent=2))
    else:
        listing = {}

    baseline = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "backend": backend,
        **throughput,
        **submit,
        **listing,
    }
    Path(args.baseline_dir).mkdir(parents=True, exist_ok=True)
    out = Path(args.baseline_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(baseline, indent=2))
    print(f"\nbaseline saved → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
