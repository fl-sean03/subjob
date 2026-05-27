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
