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


def test_submit_missing_task_file_emits_json_error(tmp_path):
    """A bad/missing --task-file must yield parseable JSON, not a traceback."""
    Pool(tmp_path / "p").init()
    missing = str(tmp_path / "nonexistent.yaml")
    r = _run(["submit", "--pool", str(tmp_path / "p"), "--task-file", missing])
    assert r.returncode == 1, r.stderr
    parsed = json.loads(r.stdout)  # must parse cleanly
    assert parsed["error"]
    assert parsed["task_file"] == missing


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


def test_failures_surfaces_artifact_detail(tmp_path):
    """A task that exits 0 but misses a declared artifact lands in failed/,
    and `subjob failures --task-id` JSON must include `artifact_detail`."""
    from subjob.worker.worker import Capabilities, Worker

    pool = Pool(tmp_path / "p")
    pool.init()
    snap = tmp_path / "snap"
    snap.mkdir()
    pool.submit(
        Task(
            id="missing-out",
            command="exit 0",
            env={"SNAP_DIR": str(snap)},
            artifacts={"expect": ["$SNAP_DIR/simulation.dcd"]},
        )
    )
    Worker(
        pool,
        Capabilities(cores=1, host="t"),
        poll_interval=0.05,
        idle_timeout_s=0.5,
    ).run()
    assert pool.status()["failed"] == 1

    r = _run(["failures", "--pool", str(tmp_path / "p"), "--task-id", "missing-out"])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    entry = parsed["failures"][0]
    assert entry["task_id"] == "missing-out"
    assert entry["artifact_validation_failed"] is True
    detail = entry["artifact_detail"]
    assert detail["missing_expect"]
    assert detail["missing_expect"][0].endswith("simulation.dcd")


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


def _land_in_done(pool, task_id, *, age_days):
    """Place a completed task YAML in done/ and backdate its mtime."""
    import os
    import time
    pool.submit(Task(id=task_id, command="echo hi"))
    src = pool.pending_dir / f"{task_id}.yaml"
    dest = pool.done_dir / f"{task_id}.yaml"
    os.rename(src, dest)
    old = time.time() - age_days * 86400
    os.utime(dest, (old, old))
    return dest


def test_archive_moves_old_done_tasks_gzipped(tmp_path):
    import gzip
    pool = Pool(tmp_path / "p")
    pool.init()
    _land_in_done(pool, "old", age_days=5)

    r = _run(["archive", "--pool", str(tmp_path / "p"), "--older-than-days", "1"])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["count"] == 1
    assert parsed["archived"] == ["old"]
    assert parsed["dry_run"] is False
    # Original removed from done/, gzipped copy lands in archive/
    assert not (pool.done_dir / "old.yaml").exists()
    gz = pool.root / "archive" / "old.yaml.gz"
    assert gz.exists()
    # The gzip is a valid, readable copy of the task YAML.
    with gzip.open(gz, "rt") as f:
        content = f.read()
    assert "id: old" in content


def test_archive_skips_recent_tasks(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    _land_in_done(pool, "fresh", age_days=0)
    r = _run(["archive", "--pool", str(tmp_path / "p"), "--older-than-days", "1"])
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["count"] == 0
    assert (pool.done_dir / "fresh.yaml").exists()


def test_archive_dry_run_moves_nothing(tmp_path):
    pool = Pool(tmp_path / "p")
    pool.init()
    _land_in_done(pool, "old", age_days=5)
    r = _run(["archive", "--pool", str(tmp_path / "p"), "--older-than-days", "1", "--dry-run"])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["count"] == 1
    assert parsed["dry_run"] is True
    # Nothing moved.
    assert (pool.done_dir / "old.yaml").exists()
    assert not (pool.root / "archive").exists()


def test_archive_include_failed(tmp_path):
    import os
    import time
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="boom", command="exit 1"))
    dest = pool.failed_dir / "boom.yaml"
    os.rename(pool.pending_dir / "boom.yaml", dest)
    old = time.time() - 5 * 86400
    os.utime(dest, (old, old))

    # Without the flag, failed/ is untouched.
    r1 = _run(["archive", "--pool", str(tmp_path / "p"), "--older-than-days", "1"])
    assert json.loads(r1.stdout)["count"] == 0
    assert dest.exists()

    r2 = _run(
        ["archive", "--pool", str(tmp_path / "p"), "--older-than-days", "1", "--include-failed"]
    )
    parsed = json.loads(r2.stdout)
    assert parsed["count"] == 1
    assert parsed["archived"] == ["boom"]
    assert not dest.exists()
    assert (pool.root / "archive" / "boom.yaml.gz").exists()


def _plant_stamped_claim(pool, task_id, worker_id):
    """Plant a task in claimed/ with a claimed_by stamp."""
    t = Task(
        id=task_id,
        command="echo hi",
        attempts=[
            {
                "claimed_by": worker_id,
                "claimed_at": "2026-05-30T00:00:00Z",
                "host": "deadhost",
            }
        ],
    )
    path = pool.claimed_dir / f"{task_id}.yaml"
    path.write_text(t.to_yaml())
    return path


def test_reap_stale_auto_with_missing_heartbeat(tmp_path):
    """--auto reaps a claim whose claimed_by has no heartbeat file."""
    pool = Pool(tmp_path / "p")
    pool.init()
    _plant_stamped_claim(pool, "orph", "dead-w0")
    # No heartbeat written → owner_missing
    r = _run([
        "reap-stale", "--pool", str(tmp_path / "p"),
        "--auto", "--older-than", "60",
    ])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["count"] == 1
    assert parsed["reaped"][0]["task_id"] == "orph"
    assert parsed["reaped"][0]["reason"] == "owner_missing"
    assert pool.status()["pending"] == 1
    assert pool.status()["claimed"] == 0


def test_reap_stale_auto_with_stale_heartbeat(tmp_path):
    """--auto reaps a claim whose owner heartbeat is older than the threshold."""
    import time as _time
    pool = Pool(tmp_path / "p")
    pool.init()
    _plant_stamped_claim(pool, "stuck", "owner-w1")
    # Write an old heartbeat (~500s ago)
    pool.write_heartbeat("owner-w1", now_ns=int((_time.time() - 500) * 1e9))
    r = _run([
        "reap-stale", "--pool", str(tmp_path / "p"),
        "--auto", "--older-than", "60",
    ])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["count"] == 1
    reason = parsed["reaped"][0]["reason"]
    assert reason.startswith("owner_stale_"), reason
    assert pool.status()["pending"] == 1


def test_reap_stale_auto_with_fresh_heartbeat_skips(tmp_path):
    """--auto must NOT reap a claim whose owner heartbeat is fresh."""
    import time as _time
    pool = Pool(tmp_path / "p")
    pool.init()
    _plant_stamped_claim(pool, "live", "owner-w2")
    # Fresh heartbeat
    pool.write_heartbeat("owner-w2", now_ns=int(_time.time() * 1e9))
    r = _run([
        "reap-stale", "--pool", str(tmp_path / "p"),
        "--auto", "--older-than", "60",
    ])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["count"] == 0
    assert pool.status()["claimed"] == 1


def test_reap_stale_auto_falls_back_to_mtime_for_legacy_claims(tmp_path):
    """A claim with NO claimed_by stamp falls back to mtime under --auto."""
    import os
    import time as _time
    pool = Pool(tmp_path / "p")
    pool.init()
    pool.submit(Task(id="legacy", command="echo hi"))
    # Move to claimed/ without any stamp; backdate mtime.
    os.rename(pool.pending_dir / "legacy.yaml", pool.claimed_dir / "legacy.yaml")
    old = _time.time() - 10000
    os.utime(pool.claimed_dir / "legacy.yaml", (old, old))
    r = _run([
        "reap-stale", "--pool", str(tmp_path / "p"),
        "--auto", "--older-than", "60",
    ])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["count"] == 1
    reason = parsed["reaped"][0]["reason"]
    assert reason.startswith("legacy_mtime_"), reason
    assert pool.status()["pending"] == 1


def test_diagnose_single_task_with_matching_prior(tmp_path):
    """`subjob diagnose --task-id` JSON includes verdict and matches."""
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_one_task(pool, "boom", "exit 7")
    (pool.root / "priors.yaml").write_text(
        "priors:\n"
        "  - id: exit-7-known\n"
        "    verdict: needs-retry\n"
        "    suggested_fix: |\n"
        "      Bump retry and resubmit.\n"
        "    match:\n"
        "      exit_code: 7\n"
    )
    r = _run(["diagnose", "--pool", str(tmp_path / "p"), "--task-id", "boom"])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["task_id"] == "boom"
    assert parsed["verdict"] == "needs-retry"
    assert len(parsed["matches"]) == 1
    assert parsed["matches"][0]["id"] == "exit-7-known"


def test_diagnose_batch_lists_all_failures(tmp_path):
    """`subjob diagnose` (no --task-id) returns a count and per-task verdicts."""
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_one_task(pool, "a", "exit 1")
    _fail_one_task(pool, "b", "exit 1")
    (pool.root / "priors.yaml").write_text(
        "priors:\n"
        "  - id: any-exit-1\n"
        "    verdict: known-failure\n"
        "    suggested_fix: |\n"
        "      Check inputs.\n"
        "    match:\n"
        "      exit_code: 1\n"
    )
    r = _run(["diagnose", "--pool", str(tmp_path / "p")])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["count"] == 2
    ids = sorted(d["task_id"] for d in parsed["diagnoses"])
    assert ids == ["a", "b"]
    assert all(d["verdict"] == "known-failure" for d in parsed["diagnoses"])


def test_diagnose_no_priors_yaml_does_not_error(tmp_path):
    """Missing priors.yaml is not an error: verdict is "unknown" with no matches."""
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_one_task(pool, "boom", "exit 7")
    # No priors.yaml written.
    r = _run(["diagnose", "--pool", str(tmp_path / "p"), "--task-id", "boom"])
    assert r.returncode == 0, r.stderr
    parsed = json.loads(r.stdout)
    assert parsed["task_id"] == "boom"
    assert parsed["verdict"] == "unknown"
    assert parsed["matches"] == []
    assert parsed["suggested_fix"] is None
    # Batch mode on the same pool also returns cleanly
    r2 = _run(["diagnose", "--pool", str(tmp_path / "p")])
    assert r2.returncode == 0, r2.stderr
    parsed2 = json.loads(r2.stdout)
    assert parsed2["count"] == 1
    assert parsed2["diagnoses"][0]["verdict"] == "unknown"


def test_diagnose_malformed_priors_yaml_emits_error(tmp_path):
    """Malformed priors.yaml surfaces a JSON error (not a traceback)."""
    pool = Pool(tmp_path / "p")
    pool.init()
    _fail_one_task(pool, "boom", "exit 7")
    (pool.root / "priors.yaml").write_text(
        "priors:\n  - verdict: missing-id\n"
    )
    r = _run(["diagnose", "--pool", str(tmp_path / "p"), "--task-id", "boom"])
    assert r.returncode == 1
    parsed = json.loads(r.stdout)
    assert "id must be a non-empty string" in parsed["error"]


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
