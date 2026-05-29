"""Tier X — real MD binary (NAMD) end-to-end on a real GPU node, via subjob.

Runs the standalone NAMD3 binary on a minimal lab-agnostic Lennard-Jones
argon system (validation/inputs/namd/) entirely through the subjob public
API: stage inputs → submit a task → ensure_workers on a GPU partition →
follow_until_done → assert the real binary finished.

The task command also probes GPU passthrough (CUDA_VISIBLE_DEVICES +
nvidia-smi), so this single GPU job validates BOTH "subjob runs a real
domain MD engine end-to-end" AND "subjob hands the task its GPU".

The NAMD3 build is multicore-CUDA and FATALs without a GPU, so this MUST
target a GPU partition. `atesting_a100` (qos `testing`) schedules fastest.

Run on Alpine:
    python -m validation.tiers.tier_10_real_binary \
        --pool-root /scratch/alpine/sefl7948/pools \
        --partition atesting_a100 --qos testing --account ucb-general
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

from subjob.lib.pool import Pool
from subjob.lib.task import Resources, Task
from validation import gates as G
from validation.report import TierReport

NAMD3 = "/projects/sefl7948/software/NAMD_3.0.2_Linux-x86_64-multicore-CUDA/namd3"
INPUTS = Path(__file__).resolve().parent.parent / "inputs" / "namd"
NAMD_FILES = ["argon.pdb", "argon.psf", "argon.params", "argon.namd"]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool-root", required=True)
    parser.add_argument("--partition", default="atesting_a100")
    parser.add_argument("--qos", default="testing")
    parser.add_argument("--account", default="ucb-general")
    parser.add_argument("--namd3", default=NAMD3)
    parser.add_argument("--timeout", type=float, default=3600)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("tier10")

    pool_root = Path(args.pool_root) / f"subjob-tier10-{int(time.time())}"
    pool = Pool(pool_root)
    pool.init()

    # Stage the NAMD inputs into a per-task workdir the task will cd into.
    workdir = pool_root / "namd-work"
    workdir.mkdir(parents=True, exist_ok=True)
    for f in NAMD_FILES:
        shutil.copy(INPUTS / f, workdir / f)
    log.info("staged NAMD inputs → %s", workdir)

    # One task: probe the GPU subjob handed it, then run the real MD binary.
    command = (
        "echo CVD=$CUDA_VISIBLE_DEVICES && "
        "nvidia-smi --query-gpu=name --format=csv,noheader && "
        f"{args.namd3} +p1 argon.namd && "
        "echo NAMD_E2E_DONE"
    )
    pool.submit(Task(
        id="namd_argon_gpu",
        command=command,
        workdir=str(workdir),
        resources=Resources(cores=2, gpus=1, walltime_seconds=600),
    ))

    handles = pool.ensure_workers(
        backend="slurm", count=1, cores=2, gpus=1,
        partition=args.partition, qos=args.qos,
        walltime_seconds=900, idle_timeout_seconds=15,
        extra_sbatch_args=[f"--account={args.account}"],
    )
    log.info("submitted GPU worker(s): %s", [h.job_id for h in handles])

    started = time.time()
    try:
        final = pool.follow_until_done(timeout_s=args.timeout, poll_interval=10.0)
        timed_out = False
    except TimeoutError as e:
        log.error("timed out: %s", e)
        final = pool.status()
        timed_out = True
    duration = time.time() - started

    report = TierReport(
        tier_name="Tier X — real MD binary (NAMD) + GPU passthrough E2E",
        pool_path=str(pool_root),
        backend="slurm",
        worker_summary=f"1 GPU worker on {args.partition}/{args.qos}",
        duration_s=duration,
    )
    report.gates += [
        G.status_done_failed(done=1, failed=0)(pool),
        G.no_double_claims()(pool),
        G.task_attempt_field("namd_argon_gpu", "exit_code", 0)(pool),
        # Real binary actually completed its run:
        G.task_stdout_contains("namd_argon_gpu", "End of program")(pool),
        G.task_stdout_contains("namd_argon_gpu", "NAMD_E2E_DONE")(pool),
        # GPU passthrough: the task saw a CUDA device (nvidia-smi printed a GPU name)
        G.task_stdout_contains("namd_argon_gpu", "CVD=")(pool),
        G.GateResult("follow_until_done_no_timeout", not timed_out, f"timed_out={timed_out}, final={final}"),
    ]
    report.write(pool_root / "REPORT.md")
    print()
    print((pool_root / "REPORT.md").read_text())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
