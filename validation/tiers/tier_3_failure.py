"""Tier III — failure injection.

Adversarial workload. Worker must classify each correctly and keep running
through all of them.

Tasks:
  segfault           — ctypes null deref; exit code via signal
  exit_1, exit_127   — varied exit code matrix
  exit_137           — equivalent to SIGKILL exit code
  exit_255           — max exit code
  infinite_stdout    — yes | head 100MB; must be in done/ with stdout_tail = 4 KB max
  sigterm_ignore     — bash trap '' TERM; SIGKILL fallback fires
  signal_grace       — bash trap echo; walltime kills but handler ran

Plus 2 ok tasks bracketing — proves worker keeps draining.
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


def submit(pool):
    # Two ok tasks bracket the failures
    pool.submit(W.echo_only("ok_before", "before failures"))
    # The failures themselves
    pool.submit(W.segfault("seg"))
    pool.submit(W.exit_code("exit_1", 1))
    pool.submit(W.exit_code("exit_127", 127))
    pool.submit(W.exit_code("exit_137", 137))
    pool.submit(W.exit_code("exit_255", 255))
    pool.submit(W.stdout_firehose("infinite_stdout", lines=50000))
    pool.submit(W.signal_ignore("sigterm_ignore"))
    pool.submit(W.signal_grace("sig_grace"))
    pool.submit(W.echo_only("ok_after", "after failures"))


PASS_IDS = {"ok_before", "ok_after", "infinite_stdout"}
FAIL_IDS = {"seg", "exit_1", "exit_127", "exit_137", "exit_255", "sigterm_ignore", "sig_grace"}


def build_gates() -> list[G.Gate]:
    return [
        G.status_done_failed(done=len(PASS_IDS), failed=len(FAIL_IDS)),
        G.all_in_state("done", PASS_IDS),
        G.all_in_state("failed", FAIL_IDS),
        G.no_double_claims(),

        # Exit code matrix
        G.task_attempt_field("exit_1", "exit_code", 1),
        G.task_attempt_field("exit_127", "exit_code", 127),
        G.task_attempt_field("exit_137", "exit_code", 137),
        G.task_attempt_field("exit_255", "exit_code", 255),

        # Segfault: depending on Python version + how shell propagates,
        # the captured exit_code is either 139 (128+SIGSEGV(11), bash) or
        # -11 (raw signal). Both indicate the same thing.
        G.task_attempt_field_in("seg", "exit_code", [139, -11]),

        # Walltime ignorers / gracefuls
        G.task_attempt_field("sigterm_ignore", "walltime_killed", True),
        G.task_attempt_field("sig_grace", "walltime_killed", True),
        G.task_stdout_contains("sig_grace", "caught SIGTERM"),

        # Worker survived all failures and produced ok_after
        G.task_attempt_field("ok_after", "exit_code", 0),
        G.task_stdout_contains("ok_after", "after failures"),

        # Infinite stdout: in done/ (it doesn't fail), only the tail captured
        G.task_attempt_field("infinite_stdout", "exit_code", 0),

        G.journal_event_count("task_submitted", len(PASS_IDS) + len(FAIL_IDS)),
        G.journal_event_count("task_claimed", len(PASS_IDS) + len(FAIL_IDS)),
    ]


def build_spec(backend, partition, qos, cores):
    return TierSpec(
        name="Tier III — failure injection",
        description="Adversarial workload; worker must classify all and survive.",
        submit=submit,
        gates=build_gates(),
        worker=WorkerSpec(
            backend=backend,
            cores=cores,
            walltime_seconds=600,
            idle_timeout_seconds=15.0,
            partition=partition,
            qos=qos,
            n_workers=1,
        ),
        poll_timeout_s=900,
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--pool-root", default=None)
    parser.add_argument("--partition", default="amilan")
    parser.add_argument("--qos", default="normal")
    parser.add_argument("--cores", type=int, default=4)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    spec = build_spec("local" if args.local else "slurm", args.partition, args.qos, args.cores)

    if args.pool_root:
        pool_root = Path(args.pool_root) / f"subjob-tier3-{int(time.time())}"
    else:
        pool_root = Path(tempfile.mkdtemp(prefix="subjob-tier3-"))

    report = run_tier(spec, pool_root)
    print()
    print((pool_root / "REPORT.md").read_text())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
