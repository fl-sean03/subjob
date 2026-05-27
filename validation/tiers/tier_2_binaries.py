"""Tier II — real binary diversity.

Lab-agnostic. Each task exercises a different real binary or compute
pattern:
  - numpy SVD          (BLAS-backed numerical compute)
  - scipy ODE          (scientific stack, integration)
  - gcc compile + run  (toolchain + C runtime)
  - awk pipeline       (Unix text processing + shell pipes)
  - mp fork            (Python multiprocessing + IPC)
  - heavy file I/O     (200 MB write/read/checksum)

Deferred (Alpine module-load chain unresolved — see PLAN.md § 6
"Real binary fails to start"): LAMMPS, GROMACS, Quantum ESPRESSO, NAMD.

Run:
    python -m validation.tiers.tier_2_binaries [--local] [--pool-root <dir>]
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
from pathlib import Path

from validation import binaries as B
from validation import gates as G
from validation.runner import TierSpec, WorkerSpec, run_tier

TASK_IDS = {"numpy_svd", "scipy_ode", "gcc_compile", "awk_pipe", "mp_fork", "file_io"}


def submit(pool):
    pool.submit(B.numpy_svd("numpy_svd"))
    pool.submit(B.scipy_ode("scipy_ode"))
    pool.submit(B.gcc_compile("gcc_compile"))
    pool.submit(B.awk_pipeline("awk_pipe"))
    pool.submit(B.multiprocessing_fork("mp_fork"))
    pool.submit(B.heavy_file_io("file_io"))


def build_gates() -> list[G.Gate]:
    return [
        # Final placement — all 6 must succeed
        G.status_done_failed(done=len(TASK_IDS), failed=0),
        G.all_in_state("done", TASK_IDS),
        G.no_double_claims(),
        # Each binary must emit its success marker (proves it actually ran)
        G.task_stdout_contains("numpy_svd", B.NUMPY_SUCCESS),
        G.task_stdout_contains("scipy_ode", B.SCIPY_SUCCESS),
        G.task_stdout_contains("gcc_compile", B.GCC_SUCCESS),
        G.task_stdout_contains("awk_pipe", B.AWK_SUCCESS),
        G.task_stdout_contains("mp_fork", B.MP_SUCCESS),
        G.task_stdout_contains("file_io", B.FILEIO_SUCCESS),
        # Each task must exit 0
        G.task_attempt_field("numpy_svd", "exit_code", 0),
        G.task_attempt_field("scipy_ode", "exit_code", 0),
        G.task_attempt_field("gcc_compile", "exit_code", 0),
        G.task_attempt_field("awk_pipe", "exit_code", 0),
        G.task_attempt_field("mp_fork", "exit_code", 0),
        G.task_attempt_field("file_io", "exit_code", 0),
        # Journal events
        G.journal_event_count("task_submitted", len(TASK_IDS)),
        G.journal_event_count("task_done", len(TASK_IDS)),
    ]


def build_spec(backend: str, partition: str | None, qos: str | None, cores: int) -> TierSpec:
    return TierSpec(
        name="Tier II — real binary diversity",
        description="6 real binaries: numpy + scipy + gcc + awk + mp + file_io",
        submit=submit,
        gates=build_gates(),
        worker=WorkerSpec(
            backend=backend,
            cores=cores,
            walltime_seconds=900,
            idle_timeout_seconds=15.0,
            partition=partition,
            qos=qos,
            n_workers=1,
        ),
        poll_timeout_s=1500,
        expected_terminal_tasks=len(TASK_IDS),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--pool-root", default=None)
    parser.add_argument("--partition", default="amilan")
    parser.add_argument("--qos", default="normal")
    parser.add_argument("--cores", type=int, default=4)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    backend = "local" if args.local else "slurm"
    spec = build_spec(backend, args.partition, args.qos, args.cores)

    if args.pool_root:
        pool_root = Path(args.pool_root) / f"subjob-tier2-{int(time.time())}"
    else:
        tmp = tempfile.mkdtemp(prefix="subjob-tier2-")
        pool_root = Path(tmp)

    report = run_tier(spec, pool_root)
    print()
    print((pool_root / "REPORT.md").read_text())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
