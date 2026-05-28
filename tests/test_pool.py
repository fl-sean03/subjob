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
