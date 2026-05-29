from __future__ import annotations

import json
import subprocess
import sys

from subjob.lib.pool import Pool
from subjob.lib.task import Task


def _run(args, *, timeout=10):
    return subprocess.run(
        [sys.executable, "-m", "subjob.client.cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_status_on_fresh_pool(tmp_path):
    Pool(tmp_path / "p").init()
    r = _run(["status", "--pool", str(tmp_path / "p")])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed == {"pending": 0, "claimed": 0, "done": 0, "failed": 0}


def test_submit_via_cli(tmp_path):
    Pool(tmp_path / "p").init()
    task_yaml = tmp_path / "task.yaml"
    Task(id="t1", command="echo hi").write(task_yaml)

    r = _run(["submit", "--pool", str(tmp_path / "p"), "--task-file", str(task_yaml)])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["task_id"] == "t1"

    r2 = _run(["status", "--pool", str(tmp_path / "p")])
    assert json.loads(r2.stdout)["pending"] == 1


def test_cancel_pending(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="t1", command="echo hi"))
    r = _run(["cancel", "--pool", str(tmp_path / "p"), "--task-id", "t1"])
    assert r.returncode == 0, r.stderr
    assert pool.status() == {"pending": 0, "claimed": 0, "done": 0, "failed": 1}


def test_cancel_unknown_task(tmp_path):
    Pool(tmp_path / "p").init()
    r = _run(["cancel", "--pool", str(tmp_path / "p"), "--task-id", "ghost"])
    assert r.returncode == 1
    assert json.loads(r.stdout)["error"]


def test_follow_streams_existing_events_and_exits_on_timeout(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="t1", command="echo hi"))
    r = _run(["follow", "--pool", str(tmp_path / "p"), "--timeout", "0.3"])
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert lines
    parsed = [json.loads(ln) for ln in lines]
    assert any(e["task_id"] == "t1" for e in parsed)


def test_text_format(tmp_path):
    Pool(tmp_path / "p").init()
    r = _run(["--format", "text", "status", "--pool", str(tmp_path / "p")])
    assert "pending: 0" in r.stdout


def test_reap_stale_moves_old_claims(tmp_path):
    import os
    import time
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="stuck", command="echo hi"))
    # Simulate a dead-worker claim: move to claimed/ and backdate mtime
    os.rename(pool.pending_dir / "stuck.yaml", pool.claimed_dir / "stuck.yaml")
    old = time.time() - 10000
    os.utime(pool.claimed_dir / "stuck.yaml", (old, old))

    r = _run(["reap-stale", "--pool", str(tmp_path / "p"), "--older-than", "3600"])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["count"] == 1
    # Task moved back to pending
    assert pool.status()["pending"] == 1
    assert pool.status()["claimed"] == 0


def _fail_one_task(pool, task_id, command):
    """Submit a failing task and drain it with an in-process worker."""
    from subjob.worker.worker import Capabilities, Worker

    pool.submit(Task(id=task_id, command=command))
    Worker(
        pool,
        Capabilities(cores=1, host="t"),
        poll_interval=0.05,
        idle_timeout_s=0.5,
    ).run()


def test_failures_lists_failed_tasks(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_one_task(pool, "boom", "exit 7")
    assert pool.status()["failed"] == 1

    r = _run(["failures", "--pool", str(tmp_path / "p")])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["count"] == 1
    assert parsed["failures"][0]["task_id"] == "boom"
    assert parsed["failures"][0]["exit_code"] == 7


def test_failures_single_task_includes_command(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_one_task(pool, "boom", "exit 7")

    r = _run(["failures", "--pool", str(tmp_path / "p"), "--task-id", "boom"])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["count"] == 1
    entry = parsed["failures"][0]
    assert entry["command"] == "exit 7"
    assert entry["exit_code"] == 7


def test_reap_stale_to_failed_marks_and_emits(tmp_path):
    import os
    import time
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="stuck", command="echo hi"))
    os.rename(pool.pending_dir / "stuck.yaml", pool.claimed_dir / "stuck.yaml")
    old = time.time() - 10000
    os.utime(pool.claimed_dir / "stuck.yaml", (old, old))

    r = _run(["reap-stale", "--pool", str(tmp_path / "p"), "--older-than", "1", "--to", "failed"])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["count"] == 1
    assert parsed["reaped"][0]["moved_to"] == "failed"
    # Lands in failed/ with a reaped marker
    assert pool.status() == {"pending": 0, "claimed": 0, "done": 0, "failed": 1}
    t = pool.read_task("failed", "stuck")
    assert t.attempts[-1]["reaped_stale"] is True
    # Emitted task_failed
    types = [e["type"] for e in pool.read_journal()]
    assert "task_failed" in types


def test_cancel_already_claimed_returns_not_cancelled(tmp_path):
    import os
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="t1", command="echo hi"))
    # Simulate the task being claimed by a worker
    os.rename(pool.pending_dir / "t1.yaml", pool.claimed_dir / "t1.yaml")

    r = _run(["cancel", "--pool", str(tmp_path / "p"), "--task-id", "t1"])
    assert r.returncode == 2
    parsed = json.loads(r.stdout)
    assert parsed["not_cancelled"] == "t1"
    assert "already claimed" in parsed["reason"]
    assert "cancelled" not in parsed
    # The claim was not touched
    assert pool.status()["claimed"] == 1


def test_reap_stale_respects_threshold(tmp_path):
    import os
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="fresh", command="echo hi"))
    os.rename(pool.pending_dir / "fresh.yaml", pool.claimed_dir / "fresh.yaml")
    # Fresh mtime → should NOT be reaped with a 1h threshold
    r = _run(["reap-stale", "--pool", str(tmp_path / "p"), "--older-than", "3600"])
    assert json.loads(r.stdout)["count"] == 0
    assert pool.status()["claimed"] == 1
