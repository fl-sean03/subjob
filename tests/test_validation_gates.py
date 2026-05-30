"""Unit tests for validation/gates.py — verify each predicate correctly
returns PASS vs FAIL on contrived inputs. Catches harness bugs that would
otherwise show up only when a real tier fails.
"""

from __future__ import annotations

from subjob.lib.pool import Pool
from subjob.lib.task import Resources, Task
from subjob.worker.worker import Capabilities, Worker
from validation import gates as G


def _setup_pool(tmp_path, mixed_outcomes=True):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="ok", command="echo ok-stdout"))
    pool.submit(Task(id="bad", command="exit 9", resources=Resources(walltime_seconds=5)))
    Worker(pool, Capabilities(cores=1, host="t"), poll_interval=0.05, idle_timeout_s=1.0).run()
    return pool


def test_status_done_failed_pass(tmp_path):
    pool = _setup_pool(tmp_path)
    r = G.status_done_failed(done=1, failed=1)(pool)
    assert r.passed, r.detail


def test_status_done_failed_fail(tmp_path):
    pool = _setup_pool(tmp_path)
    r = G.status_done_failed(done=0, failed=5)(pool)
    assert not r.passed
    assert "expected" in r.detail


def test_all_in_state_pass_and_fail(tmp_path):
    pool = _setup_pool(tmp_path)
    assert G.all_in_state("done", {"ok"})(pool).passed
    assert not G.all_in_state("done", {"ok", "ghost"})(pool).passed
    assert not G.all_in_state("done", {"ok", "bad"})(pool).passed  # "bad" is in failed/


def test_no_double_claims_pass(tmp_path):
    pool = _setup_pool(tmp_path)
    assert G.no_double_claims()(pool).passed


def test_task_attempt_field(tmp_path):
    pool = _setup_pool(tmp_path)
    assert G.task_attempt_field("bad", "exit_code", 9)(pool).passed
    assert not G.task_attempt_field("bad", "exit_code", 0)(pool).passed
    assert not G.task_attempt_field("ghost", "exit_code", 0)(pool).passed


def test_task_stdout_contains(tmp_path):
    pool = _setup_pool(tmp_path)
    assert G.task_stdout_contains("ok", "ok-stdout")(pool).passed
    assert not G.task_stdout_contains("ok", "definitely-not-there")(pool).passed


def test_journal_event_present(tmp_path):
    pool = _setup_pool(tmp_path)
    assert G.journal_event_present("worker_started")(pool).passed
    assert G.journal_event_present("task_done", "ok")(pool).passed
    assert not G.journal_event_present("absurd_event_type")(pool).passed


def test_journal_event_count(tmp_path):
    pool = _setup_pool(tmp_path)
    assert G.journal_event_count("task_submitted", 2)(pool).passed
    assert not G.journal_event_count("task_submitted", 99)(pool).passed


def test_throughput_at_least(tmp_path):
    pool = _setup_pool(tmp_path)
    # Two tasks, near-instant — should always exceed a tiny rate.
    assert G.throughput_at_least(0.01)(pool).passed
    # Absurdly high rate must fail.
    assert not G.throughput_at_least(1e6)(pool).passed


def test_concurrent_at_peak(tmp_path):
    pool = _setup_pool(tmp_path)
    assert G.concurrent_at_peak(1)(pool).passed
    # Single-core worker; impossible to have ≥3 concurrent.
    assert not G.concurrent_at_peak(3)(pool).passed
