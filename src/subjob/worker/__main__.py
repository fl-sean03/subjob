"""`python -m subjob.worker --pool <dir> [--walltime-seconds N]` — worker entry point.

This is what a backend's sbatch wrapper invokes. It runs a single Worker
against the given pool until the worker's loop exits.
"""

from __future__ import annotations

import argparse
import logging
import sys

from subjob.lib.pool import Pool
from subjob.worker.worker import Capabilities, Worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subjob-worker")
    parser.add_argument("--pool", required=True, help="Pool root directory")
    parser.add_argument("--cores", type=int, default=None)
    parser.add_argument("--gpus", type=int, default=None)
    parser.add_argument(
        "--walltime-seconds",
        type=int,
        default=None,
        help="Walltime budget; ignored if SLURM env supplies one",
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        help="Exit after this many idle seconds (default: never)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")

    pool = Pool(args.pool)
    caps = Capabilities.from_env(
        cores=args.cores, gpus=args.gpus, walltime_seconds=args.walltime_seconds
    )
    worker = Worker(
        pool,
        caps,
        poll_interval=args.poll_interval,
        idle_timeout_s=args.idle_timeout,
    )
    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
