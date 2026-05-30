"""Tests for the priors framework (lib/priors.py)."""

from __future__ import annotations

import pytest

from subjob.lib.priors import (
    Prior,
    PriorSchemaError,
    load_priors,
    match_priors,
)


def _write(path, text):
    path.write_text(text)
    return path


# ---------- load_priors ----------


def test_load_priors_missing_file_returns_empty(tmp_path):
    assert load_priors(tmp_path / "does-not-exist.yaml") == []


def test_load_priors_empty_file_returns_empty(tmp_path):
    p = _write(tmp_path / "priors.yaml", "")
    assert load_priors(p) == []


def test_load_priors_null_priors_returns_empty(tmp_path):
    p = _write(tmp_path / "priors.yaml", "priors: null\n")
    assert load_priors(p) == []


def test_load_priors_minimal_entry(tmp_path):
    p = _write(
        tmp_path / "priors.yaml",
        "priors:\n"
        "  - id: minimal\n"
        "    verdict: needs-mitigation\n",
    )
    priors = load_priors(p)
    assert len(priors) == 1
    assert priors[0].id == "minimal"
    assert priors[0].verdict == "needs-mitigation"
    assert priors[0].match == {}
    assert priors[0].auto_apply is False


def test_load_priors_missing_id_raises(tmp_path):
    p = _write(
        tmp_path / "priors.yaml",
        "priors:\n"
        "  - verdict: x\n",
    )
    with pytest.raises(PriorSchemaError, match="id must be a non-empty string"):
        load_priors(p)


def test_load_priors_empty_id_raises(tmp_path):
    p = _write(
        tmp_path / "priors.yaml",
        "priors:\n"
        '  - id: ""\n'
        "    verdict: x\n",
    )
    with pytest.raises(PriorSchemaError, match="id must be a non-empty string"):
        load_priors(p)


def test_load_priors_duplicate_id_raises(tmp_path):
    p = _write(
        tmp_path / "priors.yaml",
        "priors:\n"
        "  - id: dup\n"
        "    verdict: a\n"
        "  - id: dup\n"
        "    verdict: b\n",
    )
    with pytest.raises(PriorSchemaError, match="duplicate prior id"):
        load_priors(p)


def test_load_priors_bad_regex_raises(tmp_path):
    p = _write(
        tmp_path / "priors.yaml",
        "priors:\n"
        "  - id: badre\n"
        "    verdict: x\n"
        "    match:\n"
        '      stderr_regex: "[unclosed"\n',
    )
    with pytest.raises(PriorSchemaError, match="invalid stderr_regex"):
        load_priors(p)


def test_load_priors_unknown_match_key_raises(tmp_path):
    p = _write(
        tmp_path / "priors.yaml",
        "priors:\n"
        "  - id: foo\n"
        "    verdict: x\n"
        "    match:\n"
        "      bogus_key: 1\n",
    )
    with pytest.raises(PriorSchemaError, match="unknown match keys"):
        load_priors(p)


def test_load_priors_full_entry(tmp_path):
    p = _write(
        tmp_path / "priors.yaml",
        "priors:\n"
        "  - id: namd-restart\n"
        "    description: |\n"
        "      NAMD restart files missing.\n"
        "    match:\n"
        "      exit_code: 1\n"
        '      stderr_regex: "FATAL ERROR.*restart"\n'
        "      walltime_killed: false\n"
        "    verdict: needs-mitigation\n"
        "    suggested_fix: |\n"
        "      Re-stage the restart files.\n"
        "    auto_apply: false\n",
    )
    priors = load_priors(p)
    assert len(priors) == 1
    pr = priors[0]
    assert pr.id == "namd-restart"
    assert pr.match["exit_code"] == 1
    assert pr.match["walltime_killed"] is False
    assert "FATAL ERROR" in pr.match["stderr_regex"]
    assert pr.suggested_fix.startswith("Re-stage")
    assert pr._stderr_re is not None


# ---------- match_priors ----------


def _mk(id_, **match):
    return Prior(id=id_, verdict=f"v-{id_}", match=dict(match))


def test_match_priors_empty_catalog_returns_empty():
    assert match_priors([], exit_code=1, walltime_killed=False, stderr_tail="") == []


def test_match_priors_exit_code_match():
    priors = [_mk("a", exit_code=1), _mk("b", exit_code=2)]
    out = match_priors(priors, exit_code=1, walltime_killed=False, stderr_tail="")
    assert [p.id for p in out] == ["a"]


def test_match_priors_exit_code_none_blocks_constrained_prior():
    """A prior that constrains on exit_code can't match a None observation."""
    priors = [_mk("a", exit_code=1)]
    out = match_priors(priors, exit_code=None, walltime_killed=False, stderr_tail="")
    assert out == []


def test_match_priors_stderr_regex_match():
    priors = [_mk("a", stderr_regex="FATAL.*restart")]
    out = match_priors(
        priors,
        exit_code=1,
        walltime_killed=False,
        stderr_tail="some noise\nFATAL ERROR: cannot open restart\nmore\n",
    )
    assert [p.id for p in out] == ["a"]


def test_match_priors_stderr_regex_no_match():
    priors = [_mk("a", stderr_regex="FATAL.*restart")]
    out = match_priors(
        priors, exit_code=1, walltime_killed=False, stderr_tail="all fine here"
    )
    assert out == []


def test_match_priors_walltime_killed_match():
    priors = [_mk("a", walltime_killed=True)]
    out = match_priors(priors, exit_code=None, walltime_killed=True, stderr_tail="")
    assert [p.id for p in out] == ["a"]
    out2 = match_priors(priors, exit_code=None, walltime_killed=False, stderr_tail="")
    assert out2 == []


def test_match_priors_combined_match():
    """All conditions in `match` must hold."""
    priors = [_mk("a", exit_code=1, stderr_regex="boom")]
    # exit matches, stderr matches → hit
    out = match_priors(
        priors, exit_code=1, walltime_killed=False, stderr_tail="boom!"
    )
    assert [p.id for p in out] == ["a"]
    # exit matches, stderr does not → miss
    out2 = match_priors(
        priors, exit_code=1, walltime_killed=False, stderr_tail="quiet"
    )
    assert out2 == []
    # exit does not match → miss even if stderr would
    out3 = match_priors(
        priors, exit_code=2, walltime_killed=False, stderr_tail="boom!"
    )
    assert out3 == []


def test_match_priors_catch_all_matches_everything():
    """An empty match block (or omitted) is a catch-all."""
    priors = [_mk("catch")]  # no match conditions
    out = match_priors(priors, exit_code=42, walltime_killed=False, stderr_tail="anything")
    assert [p.id for p in out] == ["catch"]
    out2 = match_priors(priors, exit_code=None, walltime_killed=None, stderr_tail="")
    assert [p.id for p in out2] == ["catch"]


def test_match_priors_preserves_order_first_match_wins(tmp_path):
    """Priors-list order is the priority order; multi-match returns all."""
    priors = [
        _mk("first", exit_code=1),
        _mk("second", exit_code=1),
        _mk("catch"),
    ]
    out = match_priors(priors, exit_code=1, walltime_killed=False, stderr_tail="")
    # All three match; order preserved.
    assert [p.id for p in out] == ["first", "second", "catch"]
