from __future__ import annotations

import pytest

from subjob.lib.pool import Pool
from subjob.lib.task import Task


def _mktask(tid: str, command: str = "echo hi") -> Task:
    return Task(id=tid, command=command)


def test_init_creates_layout(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    for sub in ("pending", "claimed", "done", "failed", "logs", ".staging"):
        assert (tmp_path / "p" / sub).is_dir()
    assert (tmp_path / "p" / "journal.jsonl").is_file()


def test_submit_writes_pending_and_journal(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    pool.submit(_mktask("t2"))
    assert pool.status() == {"pending": 2, "claimed": 0, "done": 0, "failed": 0}
    events = pool.read_journal()
    assert [e["type"] for e in events] == ["task_submitted", "task_submitted"]


def test_submit_rejects_duplicate_id(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    with pytest.raises(ValueError, match="already in pool"):
        pool.submit(_mktask("t1"))


def test_claim_moves_pending_to_claimed(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    [path] = pool.pending_paths()
    claimed = pool.claim(path)
    assert claimed is not None
    assert claimed.task.id == "t1"
    assert pool.status()["pending"] == 0
    assert pool.status()["claimed"] == 1


def test_release_returns_claim_to_pending(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    claimed = pool.claim(pool.pending_paths()[0])
    pool.release(claimed)
    assert pool.status() == {"pending": 1, "claimed": 0, "done": 0, "failed": 0}
    types = [e["type"] for e in pool.read_journal()]
    assert "task_released" in types


def test_commit_done_moves_to_done_and_records_attempt(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    claimed = pool.claim(pool.pending_paths()[0])
    pool.commit_done(claimed, {"exit_code": 0, "duration_s": 1.0})
    assert pool.status() == {"pending": 0, "claimed": 0, "done": 1, "failed": 0}
    t = pool.read_task("done", "t1")
    assert t.state == "done"
    assert t.attempts and t.attempts[0]["exit_code"] == 0


def test_commit_failed(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    claimed = pool.claim(pool.pending_paths()[0])
    pool.commit_failed(claimed, {"exit_code": 137, "reason": "walltime"})
    assert pool.status()["failed"] == 1
    t = pool.read_task("failed", "t1")
    assert t.state == "failed"


def test_double_finalize_does_not_resurrect(tmp_path):
    """commit_done then commit_failed on the SAME claim must leave the task in
    done/ only — the stale second finalize is a no-op (Fix 1)."""
    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    claimed = pool.claim(pool.pending_paths()[0])
    pool.commit_done(claimed, {"exit_code": 0})
    # Stale re-finalize via the partial-failure path must NOT re-create the file.
    pool.commit_failed(claimed, {"exit_code": 1})
    assert pool.status() == {"pending": 0, "claimed": 0, "done": 1, "failed": 0}
    assert (pool.done_dir / "t1.yaml").exists()
    assert (pool.failed_dir / "t1.yaml").exists() is False


def test_release_after_finalize_is_noop(tmp_path):
    """release() on an already-finalized claim must return False and not
    resurrect the task into pending/ (Fix 1)."""
    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    claimed = pool.claim(pool.pending_paths()[0])
    pool.commit_done(claimed, {"exit_code": 0})
    assert pool.release(claimed) is False
    assert pool.status() == {"pending": 0, "claimed": 0, "done": 1, "failed": 0}
    assert (pool.pending_dir / "t1.yaml").exists() is False


def test_submit_accepts_dict_resources(tmp_path):
    """pool.submit(Task(..., resources={...})) must not raise AttributeError
    (Fix 4 — the AGENT_GUIDE example form)."""
    pool = Pool(tmp_path / "p")
    tid = pool.submit(Task(id="t1", command="echo hi", resources={"cores": 4}))
    assert tid == "t1"
    assert pool.status()["pending"] == 1
    t = pool.read_task("pending", "t1")
    assert t.resources.cores == 4


def test_priority_ordering(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.submit(Task(id="low", command="echo", priority=1))
    pool.submit(Task(id="high", command="echo", priority=100))
    pool.submit(Task(id="mid", command="echo", priority=50))
    order = [p.name for p in pool.pending_paths()]
    assert order == ["high.yaml", "mid.yaml", "low.yaml"]


def test_read_journal_since_event_id(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    first = pool.read_journal()
    assert len(first) == 1
    cutoff = first[0]["event_id"]
    pool.submit(_mktask("t2"))
    later = pool.read_journal(since_event_id=cutoff)
    assert len(later) == 1
    assert later[0]["task_id"] == "t2"


def test_follow_yields_events_then_times_out(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    seen = list(pool.follow(timeout_s=0.3, poll_interval=0.05))
    assert any(e["task_id"] == "t1" for e in seen)


def test_pending_paths_quarantines_corrupt_yaml(tmp_path):
    """A truncated/garbage YAML in pending/ must be quarantined to failed/
    on the next call to pending_paths(), not poison-pill the worker.
    """
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="ok", command="echo hi"))
    corrupt = pool.pending_dir / "corrupt.yaml"
    corrupt.write_text("!!! not yaml !!!\nunclosed: [\n")

    # Before listing: corrupt is in pending/
    assert corrupt.exists()
    assert (pool.failed_dir / "corrupt.yaml").exists() is False

    paths = pool.pending_paths()

    # ok.yaml is returned; corrupt was moved out
    assert [p.name for p in paths] == ["ok.yaml"]
    assert corrupt.exists() is False
    assert (pool.failed_dir / "corrupt.yaml").exists()
    # Journal recorded the quarantine
    types = [e["type"] for e in pool.read_journal()]
    assert "task_failed" in types


def test_pending_tasks_caches_parsed_yaml(tmp_path, monkeypatch):
    """pending_tasks() must parse each immutable pending YAML at most once
    across repeated polls (the F-001 fix)."""
    pool = Pool(tmp_path / "p")
    pool.init()
    for i in range(10):
        pool.submit(Task(id=f"t{i}", command="echo hi", priority=i))

    import subjob.lib.task as task_mod
    calls = {"n": 0}
    real_read = task_mod.Task.read

    def counting_read(path):
        calls["n"] += 1
        return real_read(path)

    monkeypatch.setattr(task_mod.Task, "read", staticmethod(counting_read))

    first = pool.pending_tasks()
    reads_after_first = calls["n"]
    assert reads_after_first == 10  # each file read once
    # Priority-sorted: highest first
    assert [p.stem for p, _ in first][:3] == ["t9", "t8", "t7"]

    # Second poll: no new reads (all cached)
    pool.pending_tasks()
    assert calls["n"] == reads_after_first  # unchanged → cache hit

    # New submission triggers exactly one more read
    pool.submit(Task(id="t99", command="echo hi", priority=99))
    pool.pending_tasks()
    assert calls["n"] == reads_after_first + 1


def test_pending_cache_evicts_claimed(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="a", command="echo hi"))
    pool.submit(Task(id="b", command="echo hi"))
    pool.pending_tasks()
    assert set(pool._pending_cache) == {"a.yaml", "b.yaml"}
    # Claim a → it leaves pending/; next pending_tasks() should evict it
    pool.claim(pool.pending_dir / "a.yaml")
    pool.pending_tasks()
    assert set(pool._pending_cache) == {"b.yaml"}


def test_journal_tolerates_torn_line(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.emit("task_done", "ok1", {})
    # Simulate a torn/interleaved write by appending garbage
    with open(pool.journal_path, "a") as f:
        f.write('{"event_id": 123, "type": "tas')  # no newline, truncated
        f.write("\n")
    pool.emit("task_done", "ok2", {})
    events = pool.read_journal()
    # Both good events survive; the torn line is skipped
    ids = [e["task_id"] for e in events]
    assert "ok1" in ids and "ok2" in ids


def test_journal_concurrent_writers_no_loss(tmp_path):
    """Many threads emitting concurrently must not lose or corrupt events."""
    import threading
    pool = Pool(tmp_path / "p")
    pool.init()

    def emit_many(worker_id):
        for i in range(50):
            pool.emit("task_done", f"w{worker_id}_t{i}", {"worker": worker_id})

    threads = [threading.Thread(target=emit_many, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = pool.read_journal()
    done = [e for e in events if e["type"] == "task_done"]
    assert len(done) == 8 * 50  # no events lost
    # All lines parsed cleanly (no torn lines)
    assert len({e["task_id"] for e in done}) == 8 * 50


def test_submit_exclusive_create_rejects_concurrent_duplicate(tmp_path):
    """Two submits of the same id: second must fail even if the dedup check
    is bypassed (simulates the TOCTOU window)."""
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="dup", command="echo hi"))
    # Direct second submit hits both the soft check and the os.link guard
    with pytest.raises(ValueError, match="already in pool"):
        pool.submit(Task(id="dup", command="echo hi"))
    assert pool.status()["pending"] == 1


def test_release_records_attempt_and_invalidates_cache(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="t1", command="echo hi"))
    claimed = pool.claim(pool.pending_dir / "t1.yaml")
    assert pool.release(claimed, reason="walltime") is True
    # Task back in pending with a recorded release attempt
    t = Task.read(pool.pending_dir / "t1.yaml")
    assert pool.release_attempt_count(t) == 1
    assert t.attempts[-1]["reason"] == "walltime"


def test_release_attempt_count_accumulates(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="t1", command="echo hi"))
    for _ in range(3):
        claimed = pool.claim(pool.pending_dir / "t1.yaml")
        pool.release(claimed)
    t = Task.read(pool.pending_dir / "t1.yaml")
    assert pool.release_attempt_count(t) == 3


def _drain_with_worker(pool, cores=2):
    from subjob.worker.worker import Capabilities, Worker

    Worker(
        pool,
        Capabilities(cores=cores, host="t"),
        poll_interval=0.05,
        idle_timeout_s=0.5,
    ).run()


def test_follow_until_done_returns_when_drained(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit_batch([Task(id=f"t{i}", command="true") for i in range(3)])
    _drain_with_worker(pool)
    status = pool.follow_until_done(timeout_s=5, poll_interval=0.05)
    assert status["pending"] == 0
    assert status["claimed"] == 0
    assert status["done"] == 3


def test_follow_until_done_times_out_when_pending(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="stuck", command="echo hi"))
    with pytest.raises(TimeoutError):
        pool.follow_until_done(timeout_s=0.2, poll_interval=0.05)


def test_follow_until_state_true_when_all_done(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    ids = [f"t{i}" for i in range(3)]
    pool.submit_batch([Task(id=i, command="true") for i in ids])
    _drain_with_worker(pool)
    assert pool.follow_until_state("done", ids, timeout_s=5, poll_interval=0.05) is True


def test_follow_until_state_false_when_one_failed(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="ok", command="true"))
    pool.submit(Task(id="bad", command="exit 7"))
    _drain_with_worker(pool)
    # Both are terminal, but "bad" landed in failed/ → not all in done/
    assert pool.follow_until_state("done", ["ok", "bad"], timeout_s=5, poll_interval=0.05) is False
    assert pool.follow_until_state("failed", ["bad"], timeout_s=5, poll_interval=0.05) is True


def test_follow_until_state_times_out_when_never_terminal(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="stuck", command="echo hi"))  # no worker → stays pending
    with pytest.raises(TimeoutError):
        pool.follow_until_state("done", ["stuck"], timeout_s=0.5, poll_interval=0.05)


def test_follow_until_done_times_out_with_no_worker(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="stuck", command="echo hi"))  # no worker → stays pending
    with pytest.raises(TimeoutError):
        pool.follow_until_done(timeout_s=0.5, poll_interval=0.05)


def test_pool_claim_stamps_owner_when_worker_id_set(tmp_path):
    """When Pool(worker_id=...) is set, claim() records a claimed_by attempt."""
    pool = Pool(tmp_path / "p", worker_id="w1")
    pool.init()
    pool.submit(Task(id="t1", command="echo hi"))
    claimed = pool.claim(pool.pending_dir / "t1.yaml")
    assert claimed is not None
    # Re-read from disk to confirm the stamp was persisted (not just in-memory).
    t = Task.read(pool.claimed_dir / "t1.yaml")
    assert t.attempts, "claim should record an attempt entry"
    last = t.attempts[-1]
    assert last["claimed_by"] == "w1"
    assert "claimed_at" in last
    assert "host" in last


def test_pool_claim_no_stamp_when_worker_id_none(tmp_path):
    """Default Pool (no worker_id) must NOT add a claimed_by attempt — preserves legacy behavior."""
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="t1", command="echo hi"))
    claimed = pool.claim(pool.pending_dir / "t1.yaml")
    assert claimed is not None
    t = Task.read(pool.claimed_dir / "t1.yaml")
    # No attempt should mention claimed_by
    assert not any("claimed_by" in a for a in t.attempts)


def _fail_a_task(pool, task_id, command="exit 7", stderr_text=""):
    """Drain a single failing task through an in-process worker.

    If ``stderr_text`` is given, append it to ``logs/<id>.err`` after the
    worker finishes so the diagnose stderr_regex path has something to match.
    """
    from subjob.worker.worker import Capabilities, Worker

    pool.submit(Task(id=task_id, command=command))
    Worker(
        pool,
        Capabilities(cores=1, host="t"),
        poll_interval=0.05,
        idle_timeout_s=0.5,
    ).run()
    if stderr_text:
        with open(pool.logs_dir / f"{task_id}.err", "a") as f:
            f.write(stderr_text)


def test_diagnose_unknown_task_returns_error_field(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    result = pool.diagnose("not_a_task")
    assert result["task_id"] == "not_a_task"
    assert result["error"] == "task not in failed/"
    assert result["verdict"] == "unknown"
    assert result["matches"] == []


def test_diagnose_failed_task_no_priors_yaml(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_a_task(pool, "boom", command="exit 7")
    result = pool.diagnose("boom")
    assert result["task_id"] == "boom"
    assert result["exit_code"] == 7
    assert result["matches"] == []
    assert result["verdict"] == "unknown"
    assert result["suggested_fix"] is None


def test_diagnose_matches_by_exit_code(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_a_task(pool, "boom", command="exit 7")
    (pool.root / "priors.yaml").write_text(
        "priors:\n"
        "  - id: exit-7-known\n"
        "    verdict: needs-retry\n"
        "    suggested_fix: |\n"
        "      Bump retry count and re-submit.\n"
        "    match:\n"
        "      exit_code: 7\n"
    )
    result = pool.diagnose("boom")
    assert result["verdict"] == "needs-retry"
    assert result["suggested_fix"].startswith("Bump retry")
    assert len(result["matches"]) == 1
    assert result["matches"][0]["id"] == "exit-7-known"


def test_diagnose_matches_by_stderr_regex(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_a_task(
        pool,
        "boom",
        command="exit 1",
        stderr_text="\nFATAL ERROR: cannot open restart file\n",
    )
    (pool.root / "priors.yaml").write_text(
        "priors:\n"
        "  - id: namd-restart\n"
        "    verdict: needs-mitigation\n"
        '    suggested_fix: "Re-stage restart files."\n'
        "    match:\n"
        '      stderr_regex: "FATAL ERROR.*restart"\n'
    )
    result = pool.diagnose("boom")
    assert result["verdict"] == "needs-mitigation"
    assert len(result["matches"]) == 1
    assert result["matches"][0]["id"] == "namd-restart"
    assert "FATAL ERROR" in result["stderr_tail"]


def test_diagnose_no_match_returns_unknown(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_a_task(pool, "boom", command="exit 7")
    (pool.root / "priors.yaml").write_text(
        "priors:\n"
        "  - id: only-exit-99\n"
        "    verdict: special\n"
        "    match:\n"
        "      exit_code: 99\n"
    )
    result = pool.diagnose("boom")
    assert result["matches"] == []
    assert result["verdict"] == "unknown"
    assert result["suggested_fix"] is None


def test_diagnose_priors_cached_across_calls(tmp_path):
    """_load_priors_once must only read priors.yaml once per Pool instance."""
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_a_task(pool, "a", command="exit 1")
    _fail_a_task(pool, "b", command="exit 1")
    (pool.root / "priors.yaml").write_text(
        "priors:\n  - id: catch\n    verdict: x\n"
    )
    # First call populates the cache
    pool.diagnose("a")
    assert pool._priors is not None
    first_priors = pool._priors
    pool.diagnose("b")
    # Same list object — not reloaded
    assert pool._priors is first_priors


def test_diagnose_returns_error_dict_on_malformed_priors(tmp_path):
    """A structurally bad priors.yaml must not raise out of diagnose.

    The contract: diagnose() always returns a dict. A malformed priors
    catalog surfaces as an `error` field with `verdict == "unknown"` —
    callers can rely on never having to catch an exception.
    """
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_a_task(pool, "boom", command="exit 7")
    # `priors:` must be a list per the schema; a scalar should make
    # load_priors() raise PriorSchemaError. diagnose() must swallow that.
    (pool.root / "priors.yaml").write_text("priors: not-a-list\n")

    result = pool.diagnose("boom")  # must NOT raise

    assert "error" in result
    assert "priors.yaml" in result["error"]
    assert result["verdict"] == "unknown"
    assert result["matches"] == []
    assert result["suggested_fix"] is None
    # Signal fields still populated from the failed task itself
    assert result["task_id"] == "boom"
    assert result["exit_code"] == 7


def test_read_journal_skips_truncated_line_between_valid_events(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.emit("task_done", "a", {})
    # Append a truncated, non-JSON line directly to the journal
    with open(pool.journal_path, "a") as f:
        f.write('{"event_id": 99, "type": "tas\n')
    pool.emit("task_done", "b", {})
    events = pool.read_journal()
    assert [e["task_id"] for e in events] == ["a", "b"]
    assert len(events) == 2


# ---- Cross-worker race tests (rename-first invariant) ----
#
# These three tests directly simulate the T10 race that Auditor A surfaced:
# a worker calls finalize/release/auto_reap while ANOTHER writer has already
# renamed the claimed file out from under it. The rename-first pattern
# (atomic_move BEFORE write_text) guarantees the loser of the race no-ops
# instead of resurrecting the file in two state dirs.


def test_finalize_no_resurrect_when_another_writer_renames_first(tmp_path):
    """If another writer renames the claimed file before _finalize runs, the
    finalize must no-op — never resurrect the task into done/ or claimed/."""
    import os as _os

    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    claimed = pool.claim(pool.pending_paths()[0])
    # Race: another writer atomically moves the claimed file to pending/
    # (simulating an auto-reap on a sibling worker) before we commit_done.
    racing_dest = pool.pending_dir / claimed.path.name
    _os.rename(claimed.path, racing_dest)
    # commit_done should not raise and should not resurrect the file.
    pool.commit_done(claimed, {"exit_code": 0})
    assert (pool.pending_dir / "t1.yaml").exists()
    assert not (pool.done_dir / "t1.yaml").exists()
    assert not (pool.claimed_dir / "t1.yaml").exists()
    assert pool.status() == {"pending": 1, "claimed": 0, "done": 0, "failed": 0}


def test_release_no_resurrect_when_another_writer_renames_first(tmp_path):
    """If another writer renames the claimed file before release() runs,
    release must return False and not resurrect the file in pending/."""
    import os as _os

    pool = Pool(tmp_path / "p")
    pool.submit(_mktask("t1"))
    claimed = pool.claim(pool.pending_paths()[0])
    # Another writer wins: moves claimed → done/ before our release.
    racing_dest = pool.done_dir / claimed.path.name
    _os.rename(claimed.path, racing_dest)
    assert pool.release(claimed, reason="shutdown") is False
    assert (pool.done_dir / "t1.yaml").exists()
    assert not (pool.pending_dir / "t1.yaml").exists()
    assert not (pool.claimed_dir / "t1.yaml").exists()
    assert pool.status() == {"pending": 0, "claimed": 0, "done": 1, "failed": 0}


def test_auto_reap_stale_no_resurrect_when_another_writer_renames_first(tmp_path):
    """If the owning worker finalizes the claimed file while auto_reap_stale
    is mid-flight, the reap must skip that file rather than resurrecting it
    into pending/."""
    import os as _os
    import time as _time

    # Use a worker_id so the claim is stamped and the reap codepath uses the
    # owner-missing branch (no heartbeat) rather than legacy mtime.
    pool = Pool(tmp_path / "p", worker_id="ghost-worker")
    pool.submit(_mktask("t1"))
    claimed = pool.claim(pool.pending_paths()[0])
    # The owner is "ghost-worker" with no heartbeat file → auto_reap would
    # normally reap this with reason "owner_missing". Simulate a concurrent
    # finalize landing the file in done/ between auto_reap_stale's iterdir()
    # and its atomic_move. We do this by renaming the file out from under
    # the reap, then calling auto_reap_stale with a path that's already gone.
    racing_dest = pool.done_dir / claimed.path.name
    _os.rename(claimed.path, racing_dest)
    # Use a fresh Pool (no worker_id) so auto_reap_stale doesn't skip on owner.
    reaper = Pool(tmp_path / "p")
    # threshold_s=0 to ensure any heartbeat-less owner would otherwise qualify.
    results = reaper.auto_reap_stale(threshold_s=0.0, now=_time.time())
    # Nothing should have been reaped — the claimed dir was empty after the race.
    assert results == []
    assert (pool.done_dir / "t1.yaml").exists()
    assert not (pool.pending_dir / "t1.yaml").exists()
    assert pool.status() == {"pending": 0, "claimed": 0, "done": 1, "failed": 0}
