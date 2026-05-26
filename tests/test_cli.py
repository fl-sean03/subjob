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
