from __future__ import annotations

import pytest

from subjob.lib.task import Resources, Task, validate_id


def test_minimal_task_construction():
    t = Task(id="t1", command="echo hi")
    assert t.id == "t1"
    assert t.state == "pending"
    assert t.resources.cores == 1
    assert t.created_at  # auto-stamped


def test_roundtrip_yaml():
    t = Task(
        id="snap_005",
        command="cd /scratch/x && echo hi\necho done\n",
        priority=100,
        env={"SNAP_DIR": "/scratch/x", "CORES": "4"},
        resources=Resources(cores=4, memory_gb=8, walltime_seconds=1800),
        depends_on=["build_005"],
        retry={"max_attempts": 2, "retry_on_priors": ["x", "y"]},
    )
    text = t.to_yaml()
    back = Task.from_yaml(text)
    # Compare dicts (handles env stringification etc.)
    assert back.to_dict() == t.to_dict()


def test_write_read_roundtrip(tmp_path):
    t = Task(id="t1", command="echo hi")
    p = tmp_path / "t1.yaml"
    t.write(p)
    back = Task.read(p)
    assert back.to_dict() == t.to_dict()


@pytest.mark.parametrize(
    "bad_id",
    ["", "../etc/passwd", "a b", "a/b", "a:b", "a" * 201, "$x"],
)
def test_reject_unsafe_ids(bad_id):
    with pytest.raises(ValueError):
        validate_id(bad_id)


@pytest.mark.parametrize(
    "good_id",
    ["t1", "snap_005", "Pt100-snap-001", "a.b.c", "abc-DEF_123"],
)
def test_accept_safe_ids(good_id):
    validate_id(good_id)  # does not raise


def test_reject_empty_command():
    with pytest.raises(ValueError, match="command"):
        Task(id="t1", command="")
    with pytest.raises(ValueError, match="command"):
        Task(id="t1", command="   \n  ")


def test_state_round_trip_for_failed_task():
    t = Task(
        id="t1",
        command="false",
        state="failed",
        attempts=[{"started_at": "2026-05-26T12:00:00Z", "exit_code": 1, "host": "c1"}],
    )
    back = Task.from_yaml(t.to_yaml())
    assert back.state == "failed"
    assert back.attempts == t.attempts


def test_workdir_round_trip():
    t = Task(id="t1", command="echo hi", workdir="/scratch/x")
    back = Task.from_yaml(t.to_yaml())
    assert back.workdir == "/scratch/x"


def test_workdir_default_empty():
    t = Task(id="t1", command="echo hi")
    assert t.workdir == ""
