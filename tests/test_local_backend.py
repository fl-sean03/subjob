from __future__ import annotations

import time

from subjob.backends.local import LocalBackend
from subjob.lib.pool import Pool
from subjob.lib.task import Task


def _wait_until_drained(pool, timeout_s=20.0):
    end = time.time() + timeout_s
    while time.time() < end:
        s = pool.status()
        if s["pending"] == 0 and s["claimed"] == 0:
            return s
        time.sleep(0.2)
    return pool.status()


def test_submit_worker_drains_pool(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit_batch([Task(id=f"t{i}", command="true") for i in range(3)])

    backend = LocalBackend()
    handle = backend.submit_worker(pool_dir=str(pool.root), cores=2, idle_timeout_seconds=2)
    assert handle.backend == "local"
    assert handle.metadata["pool_dir"] == str(pool.root)

    s = _wait_until_drained(pool)
    assert s["pending"] == 0
    assert s["claimed"] == 0
    assert s["done"] == 3

    # Worker should exit on idle timeout.
    end = time.time() + 10
    while time.time() < end and backend.status(handle).state == "running":
        time.sleep(0.2)
    assert backend.status(handle).state == "completed"


def test_count_workers_reflects_alive_procs(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    backend = LocalBackend()
    assert backend.count_workers(str(pool.root)) == 0

    h1 = backend.submit_worker(pool_dir=str(pool.root), cores=1, idle_timeout_seconds=2)
    h2 = backend.submit_worker(pool_dir=str(pool.root), cores=1, idle_timeout_seconds=2)
    # Both freshly spawned → alive.
    assert backend.count_workers(str(pool.root)) == 2

    backend.cancel(h1)
    # Give the terminated proc a moment to be reaped.
    end = time.time() + 5
    while time.time() < end and backend.count_workers(str(pool.root)) > 1:
        time.sleep(0.1)
    assert backend.count_workers(str(pool.root)) == 1

    backend.cancel(h2)


def test_status_unknown_for_untracked_pid(tmp_path):
    from subjob.backends.base import WorkerHandle

    backend = LocalBackend()
    st = backend.status(WorkerHandle(backend="local", job_id="999999999"))
    assert st.state == "unknown"
