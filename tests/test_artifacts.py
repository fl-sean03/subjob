from __future__ import annotations

import os

from subjob.lib.artifacts import validate_artifacts


def test_empty_artifacts_is_ok(tmp_path):
    ok, detail = validate_artifacts({}, env={}, workdir=str(tmp_path))
    assert ok is True
    assert detail == {"artifacts": "none declared"}


def test_none_artifacts_is_ok(tmp_path):
    ok, detail = validate_artifacts(None, env={}, workdir=str(tmp_path))
    assert ok is True
    assert detail == {"artifacts": "none declared"}


def test_missing_expect_fails(tmp_path):
    ok, detail = validate_artifacts(
        {"expect": ["does_not_exist.dcd"]},
        env={},
        workdir=str(tmp_path),
    )
    assert ok is False
    assert "missing_expect" in detail
    assert len(detail["missing_expect"]) == 1
    assert detail["missing_expect"][0].endswith("does_not_exist.dcd")


def test_expect_present_is_ok(tmp_path):
    (tmp_path / "simulation.dcd").write_text("frames")
    ok, detail = validate_artifacts(
        {"expect": ["simulation.dcd"]},
        env={},
        workdir=str(tmp_path),
    )
    assert ok is True
    assert "missing_expect" not in detail
    assert detail["expect_checked"][0].endswith("simulation.dcd")


def test_success_marker_file_missing_fails(tmp_path):
    ok, detail = validate_artifacts(
        {"success_marker": {"file": "run.log", "contains": "DONE"}},
        env={},
        workdir=str(tmp_path),
    )
    assert ok is False
    assert detail["success_marker_missing"] is True
    assert detail["success_marker_found"] is False


def test_success_marker_substring_absent_fails(tmp_path):
    (tmp_path / "run.log").write_text("STARTING\nrunning\n")
    ok, detail = validate_artifacts(
        {"success_marker": {"file": "run.log", "contains": "PRODUCTION COMPLETE"}},
        env={},
        workdir=str(tmp_path),
    )
    assert ok is False
    assert detail["success_marker_found"] is False
    assert detail["success_marker_contains"] == "PRODUCTION COMPLETE"


def test_success_marker_matched_is_ok(tmp_path):
    (tmp_path / "run.log").write_text("PRODUCTION COMPLETE at step 1000\n")
    ok, detail = validate_artifacts(
        {"success_marker": {"file": "run.log", "contains": "PRODUCTION COMPLETE"}},
        env={},
        workdir=str(tmp_path),
    )
    assert ok is True
    assert detail["success_marker_found"] is True


def test_env_var_expansion_in_expect(tmp_path):
    snap = tmp_path / "snap_001"
    snap.mkdir()
    (snap / "simulation.dcd").write_text("x")
    ok, detail = validate_artifacts(
        {"expect": ["$SNAP_DIR/simulation.dcd"]},
        env={"SNAP_DIR": str(snap)},
        workdir=None,
    )
    assert ok is True, detail
    assert detail["expect_checked"][0] == str(snap / "simulation.dcd")


def test_env_var_expansion_in_success_marker(tmp_path):
    snap = tmp_path / "snap_002"
    snap.mkdir()
    (snap / "run.log").write_text("PRODUCTION COMPLETE\n")
    ok, detail = validate_artifacts(
        {"success_marker": {"file": "${SNAP_DIR}/run.log", "contains": "PRODUCTION COMPLETE"}},
        env={"SNAP_DIR": str(snap)},
        workdir=None,
    )
    assert ok is True, detail
    assert detail["success_marker_path"] == str(snap / "run.log")


def test_workdir_relative_resolution(tmp_path):
    sub = tmp_path / "work"
    sub.mkdir()
    (sub / "out.json").write_text("{}")
    ok, detail = validate_artifacts(
        {"expect": ["out.json"]},
        env={},
        workdir=str(sub),
    )
    assert ok is True
    assert detail["expect_checked"][0] == str(sub / "out.json")


def test_workdir_none_uses_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out.json").write_text("{}")
    ok, _ = validate_artifacts({"expect": ["out.json"]}, env={}, workdir=None)
    assert ok is True


def test_combined_expect_and_marker_both_required(tmp_path):
    # expect missing, marker matched → fail.
    (tmp_path / "run.log").write_text("DONE\n")
    ok, detail = validate_artifacts(
        {
            "expect": ["simulation.dcd"],
            "success_marker": {"file": "run.log", "contains": "DONE"},
        },
        env={},
        workdir=str(tmp_path),
    )
    assert ok is False
    assert "missing_expect" in detail
    assert detail["success_marker_found"] is True


def test_absolute_paths_not_re_rooted(tmp_path):
    abs_file = tmp_path / "abs_out.json"
    abs_file.write_text("{}")
    ok, detail = validate_artifacts(
        {"expect": [str(abs_file)]},
        env={},
        workdir=str(tmp_path / "elsewhere"),
    )
    assert ok is True
    assert detail["expect_checked"][0] == os.path.normpath(str(abs_file))


def test_tilde_expansion_with_env_home(tmp_path):
    (tmp_path / "marker.txt").write_text("HI")
    ok, detail = validate_artifacts(
        {"expect": ["~/marker.txt"]},
        env={"HOME": str(tmp_path)},
        workdir=None,
    )
    assert ok is True
    assert detail["expect_checked"][0] == str(tmp_path / "marker.txt")


def test_expand_missing_var_left_literal(tmp_path, monkeypatch):
    """A `${VAR}` not in the task env must NOT fall back to os.environ —
    even if the worker process happens to have it set. The contract is
    "task env only" so a missing var produces a real validation failure
    rather than silently succeeding against an unintended worker-env value.
    """
    # Set the var in the worker process env; it must NOT leak through.
    monkeypatch.setenv("MISSING_VAR_42", str(tmp_path))
    # Create a file at the literal "${MISSING_VAR_42}/probe.txt" path —
    # but the validator should look at the unexpanded literal, which won't
    # exist under workdir.
    ok, detail = validate_artifacts(
        {"expect": ["${MISSING_VAR_42}/probe.txt"]},
        env={},
        workdir=str(tmp_path),
    )
    assert ok is False
    assert "missing_expect" in detail
    # The path the validator checked must still contain the literal token,
    # confirming no os.environ fallback fired.
    assert "${MISSING_VAR_42}" in detail["missing_expect"][0]


def test_expand_task_env_takes_precedence_over_worker_env(tmp_path, monkeypatch):
    """When the task env defines a var, it wins — even if os.environ has
    a different value for the same name. (Belt-and-suspenders for the
    "task env is authoritative" contract.)"""
    # Worker env has a misleading value.
    monkeypatch.setenv("SNAP_DIR", "/nonexistent/worker/path")
    # Task env points to the real dir.
    real_snap = tmp_path / "real_snap"
    real_snap.mkdir()
    (real_snap / "out.dcd").write_text("frames")
    ok, detail = validate_artifacts(
        {"expect": ["$SNAP_DIR/out.dcd"]},
        env={"SNAP_DIR": str(real_snap)},
        workdir=None,
    )
    assert ok is True, detail
    assert detail["expect_checked"][0] == str(real_snap / "out.dcd")
    # And the misleading worker-env value must NOT appear anywhere.
    assert "/nonexistent/worker/path" not in detail["expect_checked"][0]
