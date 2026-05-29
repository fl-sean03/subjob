from __future__ import annotations

import os
import signal
import time

from subjob.backends import make_backend
from subjob.backends.local import LocalBackend
from subjob.lib.pool import Pool
from subjob.lib.task import Task


def test_ensure_workers_submits_shortfall(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit_batch([Task(id=f"t{i}", command="sleep 5") for i in range(2)])

    backend = LocalBackend()
    first = pool.ensure_workers(
        backend, count=2, cores=1, idle_timeout_seconds=8, walltime_seconds=60
    )
    assert len(first) == 2

    # Both should still be alive (tasks sleep 5s, idle 8s) → no shortfall.
    second = pool.ensure_workers(
        backend, count=2, cores=1, idle_timeout_seconds=8, walltime_seconds=60
    )
    assert len(second) == 0

    for h in first + second:
        backend.cancel(h)


def test_ensure_workers_idempotent_across_throwaway_instances(tmp_path):
    """The string form builds a fresh backend each call; the filesystem
    registry must still report the first call's workers as alive."""
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit_batch([Task(id=f"t{i}", command="sleep 5") for i in range(2)])

    first = pool.ensure_workers(
        "local", count=2, cores=1, idle_timeout_seconds=8, walltime_seconds=60
    )
    assert len(first) == 2

    # New throwaway backend instance — must read the registry, not in-memory state.
    second = pool.ensure_workers(
        "local", count=2, cores=1, idle_timeout_seconds=8, walltime_seconds=60
    )
    assert len(second) == 0

    for h in first:
        try:
            os.kill(int(h.job_id), signal.SIGTERM)
        except ProcessLookupError:
            pass


def test_ensure_workers_resolves_string_backend(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    handles = pool.ensure_workers("local", count=1, cores=1, idle_timeout_seconds=1)
    assert len(handles) == 1
    assert handles[0].backend == "local"
    # Let the worker exit on idle so the test process doesn't leave it running.
    time.sleep(0.1)


def test_make_backend_unknown_raises():
    import pytest

    with pytest.raises(ValueError, match="unknown backend"):
        make_backend("nope")
