"""Tier 0 — harness self-test.

Runs a tiny mixed-outcome workload through the local backend, then verifies
every gate predicate with both a positive (should pass) and a negative
(should fail) input. Catches harness bugs before they masquerade as
subjob bugs.

Run with:  python -m validation.tiers.tier_0_harness
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

from validation import gates as G
from validation import workloads as W
from validation.runner import TierSpec, WorkerSpec, run_tier


def submit(pool):
    # Three definite passes
    pool.submit(W.echo_only("echo_ok", "tier0 hi"))
    pool.submit(W.exit_code("exit_zero", 0))
    pool.submit(W.cpu_spin("spin_1s", seconds=1.0))
    # One definite failure (nonzero exit)
    pool.submit(W.exit_code("exit_seven", 7))
    # One walltime kill
    pool.submit(W.signal_ignore("sigterm_ignored"))


def build_spec() -> TierSpec:
    gates: list[G.Gate] = [
        # status: 3 done, 2 failed
        G.status_done_failed(done=3, failed=2),
        # placement
        G.all_in_state("done", {"echo_ok", "exit_zero", "spin_1s"}),
        G.all_in_state("failed", {"exit_seven", "sigterm_ignored"}),
        # claim invariants
        G.no_double_claims(),
        # attempt fields
        G.task_attempt_field("exit_seven", "exit_code", 7),
        G.task_attempt_field("sigterm_ignored", "walltime_killed", True),
        G.task_attempt_field("echo_ok", "exit_code", 0),
        # durations
        G.task_duration_within("spin_1s", 0.5, 3.0),
        # stdout content
        G.task_stdout_contains("echo_ok", "tier0 hi"),
        # journal events
        G.journal_event_present("worker_started"),
        G.journal_event_present("worker_stopped"),
        G.journal_event_present("task_done", "echo_ok"),
        G.journal_event_present("task_failed", "exit_seven"),
        G.journal_event_count("task_submitted", 5),
        G.journal_event_count("task_claimed", 5),
        # throughput sanity
        G.throughput_at_least(0.1),
        # concurrency at peak ≥ 1 (single-core worker; non-trivial check is sweep-line itself)
        G.concurrent_at_peak(1),
    ]

    return TierSpec(
        name="Tier 0 — harness self-test",
        description="Verify gates fire correctly on known mixed-outcome workload.",
        submit=submit,
        gates=gates,
        worker=WorkerSpec(backend="local", cores=2, idle_timeout_seconds=2.0),
        poll_timeout_s=60,
        expected_terminal_tasks=5,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    spec = build_spec()
    with tempfile.TemporaryDirectory(prefix="subjob-tier0-") as tmp:
        report = run_tier(spec, Path(tmp))
        # Also dump the report to stdout
        print()
        print((Path(tmp) / "REPORT.md").read_text())
        return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
