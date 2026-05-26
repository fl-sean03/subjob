"""Generate the Phase 1 dogfood workload as task YAMLs — DOES NOT RUN THEM.

Per START_HERE §6:

    Tasks (60 total):
      Pt100-snap-001 through Pt100-snap-020
      Pt111-snap-001 through Pt111-snap-020
      Pt110-snap-001 through Pt110-snap-020
    Worker: 1× amilan c64 long QoS, 7-day walltime

Usage:
    python examples/per_snapshot_analysis_dryrun.py [pool-dir]

Default pool: /tmp/subjob-per-snap-dryrun/

The output is a pool directory full of pending/*.yaml that an agent can
inspect before doing the real submission to /scratch/alpine/...
"""

from __future__ import annotations

import sys
from pathlib import Path

from subjob.lib.pool import Pool
from subjob.lib.task import Resources, Task

SCRATCH_ROOT = "/scratch/alpine/sefl7948/hydrogenation-surfaces"
SURFACES = ["Pt100", "Pt111", "Pt110"]
SNAPS = range(1, 21)


def build_tasks() -> list[Task]:
    tasks = []
    for surface in SURFACES:
        for snap in SNAPS:
            snap_id = f"{surface}-snap-{snap:03d}"
            snap_dir = f"{SCRATCH_ROOT}/{surface}/jobs/snapshot_{snap:03d}"
            command = (
                f"cd {snap_dir} && /hydrog-run-pipeline G1G2 "
                f"--surface {surface} --snapshot {snap}"
            )
            tasks.append(
                Task(
                    id=snap_id,
                    command=command,
                    priority=100,
                    env={"SNAP_DIR": snap_dir, "SURFACE": surface},
                    resources=Resources(cores=4, memory_gb=8, walltime_seconds=1800),
                    artifacts={
                        "expect": [f"{snap_dir}/outputs/orientation_breakdown.json"],
                        "success_marker": {
                            "file": f"{snap_dir}/outputs/run.log",
                            "contains": "ANALYSIS COMPLETE",
                        },
                    },
                )
            )
    return tasks


def main():
    pool_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/subjob-per-snap-dryrun")
    pool = Pool(pool_dir)
    pool.init()
    tasks = build_tasks()
    for t in tasks:
        pool.submit(t)
    print(f"wrote {len(tasks)} tasks to {pool.pending_dir}")
    print("inspect with: ls -1 " + str(pool.pending_dir))
    print("or:           cat " + str(pool.pending_dir / "Pt100-snap-001.yaml"))
    print()
    print("when ready to actually run on Alpine, launch a worker against this pool:")
    print(f"  sbatch <sbatch-wrapper>  # eventually subjob backends/slurm.py auto-generates this")
    print(f"  # or for local testing:  python -m subjob.worker --pool {pool.root}")


if __name__ == "__main__":
    main()
