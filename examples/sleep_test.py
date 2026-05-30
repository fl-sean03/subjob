"""End-to-end smoke test: 5 trivial sleep tasks, one worker, all land in done/.

Usage:
    python examples/sleep_test.py [pool-dir]

Validates the round trip: Pool.submit → atomic claim → runner subprocess →
journal events → commit_done. Runs everything in-process (no sbatch).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from subjob.lib.pool import Pool
from subjob.lib.task import Resources, Task
from subjob.worker.worker import Capabilities, Worker


def main():
    if len(sys.argv) > 1:
        pool_dir = Path(sys.argv[1])
        cleanup = False
    else:
        pool_dir = Path(tempfile.mkdtemp(prefix="subjob-sleep-test-"))
        cleanup = True

    print(f"pool: {pool_dir}")
    pool = Pool(pool_dir)
    pool.init()

    n = 5
    for i in range(n):
        pool.submit(
            Task(
                id=f"sleep_{i:02d}",
                command=f"sleep 0.5 && echo task {i} done",
                resources=Resources(cores=1, walltime_seconds=30),
            )
        )

    print(f"submitted {n} tasks; starting worker (4 cores)…")
    worker = Worker(
        pool,
        Capabilities(cores=4, host="local"),
        poll_interval=0.1,
        idle_timeout_s=2.0,
    )
    worker.run()

    status = pool.status()
    print(f"final status: {status}")
    assert status["done"] == n, f"expected {n} done, got {status}"
    print("✓ sleep_test passed")

    if cleanup:
        shutil.rmtree(pool_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
