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
    pool.submit_batch([Task(id=f"t{i}", command="sleep 3") for i in range(2)])

    backend = LocalBackend()
    first = pool.ensure_workers(backend, count=2, cores=1, idle_timeout_seconds=5)
    assert len(first) == 2

    # Give workers a moment to actually start running the tasks, then confirm
    # the second call sees them alive (busy on sleep-3) → no shortfall. The
    # short sleep here is what catches workers that exit immediately (e.g. a
    # walltime/safety-margin regression) rather than staying to do work.
    time.sleep(1.0)
    second = pool.ensure_workers(backend, count=2, cores=1, idle_timeout_seconds=5)
    assert len(second) == 0, "workers should still be alive doing work"

    # And they must actually DO the work: the pool drains to done.
    final = pool.follow_until_done(timeout_s=30, poll_interval=0.5)
    assert final == {"pending": 0, "claimed": 0, "done": 2, "failed": 0}


def test_ensure_workers_idempotent_across_throwaway_instances(tmp_path):
    """The string form builds a fresh backend each call; the filesystem
    registry must still report the first call's workers as alive."""
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit_batch([Task(id=f"t{i}", command="sleep 4") for i in range(2)])

    first = pool.ensure_workers("local", count=2, cores=1, idle_timeout_seconds=6)
    assert len(first) == 2

    # Let the workers start + begin their tasks, then a NEW throwaway backend
    # instance must still see them alive via the filesystem registry.
    time.sleep(1.0)
    second = pool.ensure_workers("local", count=2, cores=1, idle_timeout_seconds=6)
    assert len(second) == 0, "registry must report the first call's live workers"

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
