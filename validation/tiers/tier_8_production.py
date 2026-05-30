"""Tier VIII — production readiness.

Scenarios that model real-deployment hazards our trivial echo/sleep tiers
never exercised. Run per-scenario:

    python -m validation.tiers.tier_8_production a   # SIGTERM-preemption release→reclaim
    python -m validation.tiers.tier_8_production b   # max-attempts cap on a never-fitting task
    python -m validation.tiers.tier_8_production c   # reap-stale dead-worker recovery
    python -m validation.tiers.tier_8_production l   # workdir contract

These run locally (orchestrating worker subprocesses) — they test logic,
not cluster behavior. The SLURM-level equivalent of (a) is "scancel a
worker mid-task"; the local SIGTERM models the same signal path.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from subjob.lib.pool import Pool
from subjob.lib.task import Resources, Task
from validation import gates as G
from validation.report import TierReport

log = logging.getLogger("tier8")


def _spawn_worker(pool_root, idle_timeout, extra=()):
    return subprocess.Popen(
        [sys.executable, "-m", "subjob.worker", "--pool", str(pool_root),
         "--cores", "2", "--poll-interval", "0.2", "--idle-timeout", str(idle_timeout),
         "--log-level", "WARNING", *extra],
    )


# ---------- A: SIGTERM preemption mid-task → release → reclaim ----------


def scenario_a(pool_root: Path):
    """Worker claims a long task, gets SIGTERM (SLURM-preemption analogue),
    must release the claim (not fail it), and a fresh worker completes it."""
    pool = Pool(pool_root)
    pool.init()
    pool.submit(Task(
        id="long", command="echo started; sleep 30; echo finished",
        resources=Resources(cores=1, walltime_seconds=120),
    ))

    log.info("starting worker A")
    a = _spawn_worker(pool_root, idle_timeout=120)
    # Wait until the task is actually claimed + running
    for _ in range(50):
        if pool.status()["claimed"] == 1:
            break
        time.sleep(0.2)
    time.sleep(1.0)  # let the subprocess get into its sleep
    log.info("status before SIGTERM: %s", pool.status())
    log.info("sending SIGTERM to worker A (preemption analogue)")
    a.terminate()
    a.wait(timeout=30)  # must NOT hang — the fix kills the in-flight subprocess
    log.info("worker A exited rc=%s; status: %s", a.returncode, pool.status())

    # Worker B should reclaim the released task and finish it
    log.info("starting worker B")
    b = _spawn_worker(pool_root, idle_timeout=5)
    b.wait(timeout=60)
    log.info("final status: %s", pool.status())

    gates = [
        G.status_done_failed(done=1, failed=0),
        G.journal_event_present("task_released", "long"),
        G.journal_event_present("task_done", "long"),
        G.task_stdout_contains("long", "finished"),
        # Two worker_started events (A and B)
        G.journal_event_count("worker_started", 2),
    ]
    return pool, gates, "Tier VIII.A — SIGTERM preemption release→reclaim"


# ---------- B: max-attempts cap ----------


def scenario_b(pool_root: Path):
    """A task that can never complete in the worker's budget must be failed
    after max_attempts releases, not bounce forever."""
    pool = Pool(pool_root)
    pool.init()
    # Pre-seed a task already at its release cap.
    pool.submit(Task(
        id="never_fits", command="echo hi",
        retry={"max_attempts": 2},
        attempts=[{"released": True}, {"released": True}],
    ))
    w = _spawn_worker(pool_root, idle_timeout=5)
    w.wait(timeout=30)
    gates = [
        G.status_done_failed(done=0, failed=1),
        G.all_in_state("failed", {"never_fits"}),
        G.journal_event_present("task_failed", "never_fits"),
    ]
    return pool, gates, "Tier VIII.B — max-attempts cap"


# ---------- C: reap-stale dead-worker recovery ----------


def scenario_c(pool_root: Path):
    """A claim orphaned by a dead worker is recovered by `subjob reap-stale`."""
    pool = Pool(pool_root)
    pool.init()
    pool.submit(Task(id="orphan", command="echo recovered", resources=Resources(walltime_seconds=30)))
    # Simulate dead-worker orphan: move to claimed/ + backdate mtime.
    os.rename(pool.pending_dir / "orphan.yaml", pool.claimed_dir / "orphan.yaml")
    old = time.time() - 100000
    os.utime(pool.claimed_dir / "orphan.yaml", (old, old))

    # Operator runs reap-stale → back to pending
    subprocess.run(
        [sys.executable, "-m", "subjob.client.cli", "reap-stale",
         "--pool", str(pool_root), "--older-than", "3600", "--to", "pending"],
        check=True, capture_output=True,
    )
    # A worker then completes it
    w = _spawn_worker(pool_root, idle_timeout=5)
    w.wait(timeout=30)
    gates = [
        G.status_done_failed(done=1, failed=0),
        G.journal_event_present("task_released", "orphan"),
        G.task_stdout_contains("orphan", "recovered"),
    ]
    return pool, gates, "Tier VIII.C — reap-stale dead-worker recovery"


# ---------- L: workdir contract ----------


def scenario_l(pool_root: Path):
    """A task with a declared workdir runs there."""
    pool = Pool(pool_root)
    pool.init()
    workdir = pool_root / "task-cwd"
    workdir.mkdir()
    (workdir / "marker.txt").write_text("here")
    pool.submit(Task(
        id="wd", command="pwd && ls marker.txt", workdir=str(workdir),
        resources=Resources(walltime_seconds=30),
    ))
    w = _spawn_worker(pool_root, idle_timeout=5)
    w.wait(timeout=30)
    gates = [
        G.status_done_failed(done=1, failed=0),
        G.task_stdout_contains("wd", "task-cwd"),
        G.task_stdout_contains("wd", "marker.txt"),
    ]
    return pool, gates, "Tier VIII.L — workdir contract"


SCENARIOS = {"a": scenario_a, "b": scenario_b, "c": scenario_c, "l": scenario_l}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=list(SCENARIOS))
    parser.add_argument("--pool-root", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    base = Path(args.pool_root) if args.pool_root else Path("/tmp")
    pool_root = base / f"subjob-tier8{args.scenario}-{int(time.time())}"
    pool_root.mkdir(parents=True, exist_ok=True)

    pool, gates, name = SCENARIOS[args.scenario](pool_root)
    report = TierReport(
        tier_name=name, pool_path=str(pool_root), backend="local",
        worker_summary="orchestrated worker subprocesses", duration_s=0.0,
    )
    for g in gates:
        report.gates.append(g(pool))
    report.write(pool_root / "REPORT.md")
    print((pool_root / "REPORT.md").read_text())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
