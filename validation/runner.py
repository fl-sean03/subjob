"""Tier runner — submits a tier's tasks + worker(s), polls, evaluates gates.

Two backends:
  - 'local': runs an in-process Worker. For Tier 0 + cheap synthetic Tier I bits.
  - 'slurm': submits sbatch worker(s) via SlurmBackend, polls squeue until done.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from subjob.backends.slurm import SlurmBackend
from subjob.lib.pool import Pool
from subjob.worker.worker import Capabilities, Worker
from validation.gates import Gate
from validation.report import TierReport

log = logging.getLogger("validation.runner")


@dataclass
class WorkerSpec:
    backend: str  # 'local' or 'slurm'
    cores: int = 1
    gpus: int = 0
    walltime_seconds: int = 1800
    idle_timeout_seconds: float = 15.0
    partition: str | None = None
    qos: str | None = None
    n_workers: int = 1
    extra_sbatch_args: list[str] = field(default_factory=list)


@dataclass
class TierSpec:
    name: str
    description: str
    submit: Callable[[Pool], None]  # submits tasks to the pool
    gates: list[Gate]
    worker: WorkerSpec
    poll_timeout_s: float = 1800
    # If set, runner waits until pool has at least this many tasks in (done+failed)
    expected_terminal_tasks: int | None = None


def run_tier(spec: TierSpec, pool_root: Path) -> TierReport:
    pool_root.mkdir(parents=True, exist_ok=True)
    pool = Pool(pool_root)
    pool.init()
    log.info("tier %s: submitting tasks to %s", spec.name, pool_root)
    spec.submit(pool)
    initial_status = pool.status()
    log.info("tier %s: pool initial status %s", spec.name, initial_status)

    started_at = time.time()

    if spec.worker.backend == "local":
        worker_summary = _run_local_worker(pool, spec.worker)
    elif spec.worker.backend == "slurm":
        worker_summary = _run_slurm_workers(pool, spec)
    else:
        raise ValueError(f"unknown backend: {spec.worker.backend}")

    duration = time.time() - started_at

    report = TierReport(
        tier_name=spec.name,
        pool_path=str(pool_root),
        backend=spec.worker.backend,
        worker_summary=worker_summary,
        duration_s=duration,
    )
    log.info("tier %s: evaluating %d gates", spec.name, len(spec.gates))
    for g in spec.gates:
        report.gates.append(g(pool))
    report.write(pool_root / "REPORT.md")
    log.info("tier %s: %s", spec.name, report.summary())
    return report


# ---------- local backend ----------


def _run_local_worker(pool: Pool, spec: WorkerSpec) -> str:
    caps = Capabilities(cores=spec.cores, gpus=spec.gpus, host="local")
    worker = Worker(
        pool,
        caps,
        poll_interval=0.1,
        idle_timeout_s=spec.idle_timeout_seconds,
    )
    worker.run()
    return f"1 local worker ({spec.cores} cores)"


# ---------- slurm backend ----------


def _run_slurm_workers(pool: Pool, spec: TierSpec) -> str:
    backend = SlurmBackend(python_executable=sys.executable)
    handles = []
    for _ in range(spec.worker.n_workers):
        h = backend.submit_worker(
            pool_dir=str(pool.root),
            cores=spec.worker.cores,
            gpus=spec.worker.gpus,
            walltime_seconds=spec.worker.walltime_seconds,
            partition=spec.worker.partition,
            qos=spec.worker.qos,
            idle_timeout_seconds=spec.worker.idle_timeout_seconds,
            extra_sbatch_args=spec.worker.extra_sbatch_args or None,
        )
        handles.append(h)
        log.info("submitted sbatch job %s", h.job_id)
    summary = f"{len(handles)} sbatch worker(s) on {spec.worker.partition or 'default'} ({spec.worker.cores} cores each)"
    _wait_for_jobs([h.job_id for h in handles], spec.poll_timeout_s)
    return summary


def _wait_for_jobs(job_ids: list[str], timeout_s: float) -> None:
    start = time.time()
    while True:
        if not job_ids:
            return
        joined = ",".join(job_ids)
        result = subprocess.run(
            ["squeue", "-h", "-j", joined, "-o", "%T"],
            capture_output=True,
            text=True,
        )
        active = [ln for ln in result.stdout.splitlines() if ln.strip()]
        if not active:
            return
        if time.time() - start > timeout_s:
            log.warning("timeout waiting for jobs %s; cancelling", job_ids)
            subprocess.run(["scancel", *job_ids], capture_output=True)
            return
        time.sleep(10)
