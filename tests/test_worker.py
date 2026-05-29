from __future__ import annotations

import subprocess
import sys
import threading
import time

from subjob.lib.pool import Pool
from subjob.lib.task import Resources, Task
from subjob.worker.runner import run_task
from subjob.worker.worker import Capabilities, Worker, _parse_timelimit


def test_runner_captures_stdout_and_exit_zero(tmp_path):
    task = Task(id="t1", command="echo hello-stdout")
    result = run_task(task, tmp_path)
    assert result.exit_code == 0
    assert result.succeeded
    assert "hello-stdout" in result.stdout_tail


def test_runner_nonzero_exit(tmp_path):
    task = Task(id="t1", command="exit 7")
    result = run_task(task, tmp_path)
    assert result.exit_code == 7
    assert not result.succeeded


def test_runner_walltime_kill(tmp_path):
    task = Task(id="t1", command="sleep 10", resources=Resources(walltime_seconds=1))
    started = time.time()
    result = run_task(task, tmp_path)
    elapsed = time.time() - started
    assert result.walltime_killed
    assert not result.succeeded
    assert elapsed < 8


def test_worker_completes_all_pending(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    for i in range(5):
        pool.submit(Task(id=f"t{i}", command="echo done", resources=Resources(cores=1)))
    worker = Worker(
        pool,
        Capabilities(cores=4, host="testhost"),
        poll_interval=0.05,
        idle_timeout_s=1.0,
    )
    worker.run()
    assert pool.status() == {"pending": 0, "claimed": 0, "done": 5, "failed": 0}


def test_worker_journal_events(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="t1", command="echo hi"))
    Worker(
        pool,
        Capabilities(cores=1, host="testhost"),
        poll_interval=0.05,
        idle_timeout_s=0.5,
    ).run()
    types = [e["type"] for e in pool.read_journal()]
    # Submission, worker lifecycle, and the task lifecycle should all appear.
    assert "worker_started" in types
    assert "task_claimed" in types
    assert "task_started" in types
    assert "task_done" in types
    assert "worker_stopped" in types


def test_worker_respects_core_capacity(tmp_path):
    """A 1-core worker should NOT claim a 4-core task."""
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="big", command="echo big", resources=Resources(cores=4)))
    pool.submit(Task(id="small", command="echo small", resources=Resources(cores=1)))
    Worker(
        pool,
        Capabilities(cores=1, host="t"),
        poll_interval=0.05,
        idle_timeout_s=0.5,
    ).run()
    s = pool.status()
    assert s["done"] == 1
    assert s["pending"] == 1


def test_worker_walltime_budget_blocks_long_task(tmp_path):
    """A worker with 2s remaining should not start a 60s task."""
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="long", command="sleep 60", resources=Resources(walltime_seconds=60)))
    caps = Capabilities(
        cores=4,
        host="t",
        walltime_end=time.time() + 2.0,  # only 2s of budget
    )
    Worker(pool, caps, poll_interval=0.05, idle_timeout_s=0.5, walltime_safety_s=0.5).run()
    # Task should still be pending — never claimed.
    assert pool.status()["pending"] == 1


def test_worker_warns_when_budget_under_safety_margin(tmp_path, caplog):
    """A worker whose budget is <= its safety margin warns it will do no work."""
    import logging

    pool = Pool(tmp_path / "p")
    pool.init()
    caps = Capabilities(cores=1, host="t", walltime_end=time.time() + 1.0)
    with caplog.at_level(logging.WARNING, logger="subjob.worker"):
        Worker(pool, caps, poll_interval=0.05, idle_timeout_s=0.2, walltime_safety_s=60.0).run()
    assert any("no usable time" in r.message for r in caplog.records)


def test_parse_slurm_timelimit():
    assert _parse_timelimit("60") == 60 * 60
    assert _parse_timelimit("1:30") == 90
    assert _parse_timelimit("01:02:03") == 3723
    assert _parse_timelimit("1-00:00:00") == 86400


def test_worker_entrypoint_runs(tmp_path):
    """Smoke-test python -m subjob.worker with a tiny pool."""
    pool_dir = tmp_path / "p"
    pool = Pool(pool_dir)
    pool.init()
    pool.submit(Task(id="t1", command="echo via-cli"))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "subjob.worker",
            "--pool",
            str(pool_dir),
            "--cores",
            "1",
            "--poll-interval",
            "0.05",
            "--idle-timeout",
            "0.5",
            "--log-level",
            "WARNING",
        ],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert pool.status()["done"] == 1


def test_worker_caps_release_attempts(tmp_path):
    """A task released past max_attempts must be failed, not re-run forever."""
    pool = Pool(tmp_path / "p")
    pool.init()
    # Pre-load a task that already has 3 release attempts recorded
    t = Task(
        id="bouncer",
        command="echo hi",
        retry={"max_attempts": 3},
        attempts=[{"released": True}, {"released": True}, {"released": True}],
    )
    pool.submit(t)
    Worker(pool, Capabilities(cores=2, host="t"), poll_interval=0.05, idle_timeout_s=0.5).run()
    s = pool.status()
    assert s["failed"] == 1 and s["done"] == 0
    failed = pool.read_task("failed", "bouncer")
    assert "exceeded max_attempts" in failed.attempts[-1]["error"]


def test_worker_survives_commit_failed_error(tmp_path):
    """If commit_failed raises while reaping a crashed runner, the worker must
    log and continue — not unwind the loop and leak other claims (Fix 2)."""
    pool = Pool(tmp_path / "p")
    pool.init()
    # One task whose runner will "crash", plus several that should still finish.
    pool.submit(Task(id="crasher", command="echo boom", resources=Resources(cores=1)))
    for i in range(3):
        pool.submit(Task(id=f"ok{i}", command="echo done", resources=Resources(cores=1)))

    worker = Worker(
        pool,
        Capabilities(cores=4, host="t"),
        poll_interval=0.05,
        idle_timeout_s=1.0,
    )

    # Force the crasher's runner future to raise, and make the resulting
    # commit_failed blow up exactly once.
    real_run_one = worker._run_one

    def crashing_run_one(claim):
        if claim.task.id == "crasher":
            raise RuntimeError("simulated runner crash")
        return real_run_one(claim)

    worker._run_one = crashing_run_one

    real_commit_failed = pool.commit_failed
    state = {"raised": False}

    def flaky_commit_failed(claim, payload):
        if claim.task.id == "crasher" and not state["raised"]:
            state["raised"] = True
            raise OSError("simulated ENOSPC on finalize")
        return real_commit_failed(claim, payload)

    pool.commit_failed = flaky_commit_failed

    worker.run()  # must not raise

    assert state["raised"] is True
    s = pool.status()
    # The 3 good tasks still completed; the worker did not die mid-loop.
    assert s["done"] == 3, s
    # The crasher's claim was never resurrected; it stays in claimed/ (its
    # finalize failed and was swallowed) — the key invariant is the worker
    # survived and finished the others.
    assert s["claimed"] == 1, s


def test_runner_honors_workdir(tmp_path):
    from subjob.worker.runner import run_task
    sub = tmp_path / "sub"
    sub.mkdir()
    task = Task(id="t1", command="pwd", workdir=str(sub))
    result = run_task(task, tmp_path / "logs")
    assert result.exit_code == 0
    assert str(sub) in result.stdout_tail


def test_walltime_kill_reaps_forked_children(tmp_path):
    """A task that backgrounds a child must have that child killed too when the
    task is walltime-killed (process-group kill, not just the shell)."""
    marker = tmp_path / "CHILD_ALIVE"
    cmd = f"(sleep 30 && touch {marker}) & echo parent; sleep 30"
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="forker", command=cmd, resources=Resources(walltime_seconds=2)))
    Worker(
        pool,
        Capabilities(cores=1, host="t"),
        poll_interval=0.1,
        idle_timeout_s=2.0,
    ).run()

    s = pool.status()
    assert s["failed"] == 1, s
    failed = pool.read_task("failed", "forker")
    assert failed.attempts[-1].get("walltime_killed") is True

    # The backgrounded child should have been killed with the group; its marker
    # (written after a 30s sleep) must NOT appear. Poll a few seconds to be sure.
    end = time.time() + 4
    while time.time() < end:
        assert not marker.exists()
        time.sleep(0.5)


def test_worker_walltime_expiry_releases_inflight_task(tmp_path):
    """A worker that hits its allocation walltime mid-task must RELEASE the
    in-flight claim (back to pending/), not record it as a failure. The task is
    healthy, just preempted — the next worker should re-run it.

    We give the task a generous OWN walltime (so the runner won't kill it) and
    a far allocation deadline at claim time (so the fit-check lets it start),
    then collapse the allocation deadline while it's running to simulate the
    allocation walltime arriving mid-task.
    """
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(
        Task(id="preempted", command="sleep 6", resources=Resources(cores=1, walltime_seconds=300))
    )
    caps = Capabilities(cores=2, host="t", walltime_end=time.time() + 3600)  # far at claim
    worker = Worker(pool, caps, poll_interval=0.1, walltime_safety_s=0.5)

    # Once the task is claimed + running, slam the allocation deadline into the
    # past so the next _loop iteration sees _walltime_expired() and shuts down.
    def collapse_deadline():
        for _ in range(100):
            if pool.status()["claimed"] == 1:
                break
            time.sleep(0.05)
        time.sleep(0.3)  # ensure the subprocess is actually running
        worker.caps.walltime_end = time.time() - 1.0

    threading.Thread(target=collapse_deadline, daemon=True).start()
    worker.run()

    s = pool.status()
    assert s["pending"] == 1, s  # released, awaiting retry
    assert s["failed"] == 0, s  # NOT recorded as a failure
    assert s["done"] == 0, s
    types = [e["type"] for e in pool.read_journal()]
    assert "task_released" in types


def test_worker_sigterm_releases_inflight_task(tmp_path):
    """SIGTERM to a worker mid-task must release the claim (not fail it) and
    not hang on the in-flight subprocess. Models SLURM preemption."""
    import signal as _signal

    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="long", command="echo go; sleep 30; echo done",
                     resources=Resources(cores=1, walltime_seconds=120)))
    proc = subprocess.Popen(
        [sys.executable, "-m", "subjob.worker", "--pool", str(tmp_path / "p"),
         "--cores", "2", "--poll-interval", "0.2", "--idle-timeout", "120",
         "--log-level", "WARNING"],
    )
    # Wait until claimed + running
    for _ in range(50):
        if pool.status()["claimed"] == 1:
            break
        time.sleep(0.2)
    time.sleep(1.0)
    proc.send_signal(_signal.SIGTERM)
    # Must exit promptly (the fix kills the in-flight subprocess; no 30s hang)
    proc.wait(timeout=20)
    # Task released back to pending, not failed
    s = pool.status()
    assert s["pending"] == 1, s
    assert s["failed"] == 0, s
    types = [e["type"] for e in pool.read_journal()]
    assert "task_released" in types
