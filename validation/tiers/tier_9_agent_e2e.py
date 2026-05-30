"""Tier IX.E2E — the full intended agent fan-out via the PUBLIC API only.

Mirrors docs/AGENT_GUIDE.md's canonical pattern with zero manual
sbatch/squeue:

    pool = Pool(...)
    ids  = pool.submit_batch([... 18 ok + 2 failing ...])
    pool.ensure_workers(backend="slurm", count=2, cores=4, ...)
    final = pool.follow_until_done(timeout_s=1800)

then `subjob failures` must surface exactly the 2 failures.

This is the "an agent could drive subjob straight from the guide" proof —
no prior tier exercised ensure_workers / follow_until_done on real SLURM.

Run:
    python -m validation.tiers.tier_9_agent_e2e --pool-root /scratch/.../pools [--local]
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from subjob.lib.pool import Pool
from validation import gates as G
from validation import workloads as W
from validation.report import TierReport

OK_IDS = {f"ok_{i:02d}" for i in range(18)}
FAIL_IDS = {"boom_7", "boom_9"}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool-root", required=True)
    parser.add_argument("--local", action="store_true", help="use LocalBackend instead of slurm")
    parser.add_argument("--partition", default="amilan")
    parser.add_argument("--qos", default="normal")
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("tier9")

    pool_root = Path(args.pool_root) / f"subjob-tier9-{int(time.time())}"
    pool = Pool(pool_root)
    pool.init()

    # --- the agent flow, public API only ---
    tasks = [W.sleep_random(f"ok_{i:02d}", lo=0.5, hi=2.0, seed=i) for i in range(18)]
    tasks.append(W.exit_code("boom_7", 7))
    tasks.append(W.exit_code("boom_9", 9))
    ids = pool.submit_batch(tasks)
    log.info("submitted %d tasks", len(ids))

    backend = "local" if args.local else "slurm"
    worker_kwargs = dict(cores=4, idle_timeout_seconds=20, walltime_seconds=600)
    if not args.local:
        worker_kwargs.update(partition=args.partition, qos=args.qos)
    handles = pool.ensure_workers(backend=backend, count=args.count, **worker_kwargs)
    log.info("ensure_workers launched %d worker(s): %s",
             len(handles), [h.job_id for h in handles])

    started = time.time()
    try:
        final = pool.follow_until_done(timeout_s=args.timeout, poll_interval=5.0)
        timed_out = False
    except TimeoutError as e:
        log.error("follow_until_done timed out: %s", e)
        final = pool.status()
        timed_out = True
    duration = time.time() - started

    # --- triage via the real CLI ---
    failures_cli = subprocess.run(
        [sys.executable, "-m", "subjob.client.cli", "failures", "--pool", str(pool_root)],
        capture_output=True, text=True,
    )
    try:
        failures = json.loads(failures_cli.stdout)
    except json.JSONDecodeError:
        failures = {"failures": [], "count": -1}
    failed_ids_from_cli = {f["task_id"] for f in failures.get("failures", [])}

    # --- gates ---
    gates = [
        G.status_done_failed(done=18, failed=2),
        G.all_in_state("done", OK_IDS),
        G.all_in_state("failed", FAIL_IDS),
        G.no_double_claims(),
        G.task_attempt_field("boom_7", "exit_code", 7),
        G.task_attempt_field("boom_9", "exit_code", 9),
    ]
    report = TierReport(
        tier_name="Tier IX — agent end-to-end (public API)",
        pool_path=str(pool_root),
        backend=backend,
        worker_summary=f"ensure_workers({backend}, count={args.count}) → {len(handles)} handles",
        duration_s=duration,
    )
    for g in gates:
        report.gates.append(g(pool))

    # CLI-failures gate (constructed inline so the detail is informative)
    cli_ok = failed_ids_from_cli == FAIL_IDS
    report.gates.append(
        G.GateResult(
            "subjob_failures_lists_exactly_the_failures",
            cli_ok,
            f"CLI failed ids={sorted(failed_ids_from_cli)} expected={sorted(FAIL_IDS)}",
        )
    )
    report.gates.append(
        G.GateResult("follow_until_done_did_not_time_out", not timed_out,
                     f"timed_out={timed_out}, final={final}")
    )
    report.gates.append(
        G.GateResult("ensure_workers_returned_handles", len(handles) >= 1,
                     f"handles={len(handles)}")
    )

    report.write(pool_root / "REPORT.md")
    print()
    print((pool_root / "REPORT.md").read_text())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
