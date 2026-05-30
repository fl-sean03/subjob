from __future__ import annotations

import pytest

from subjob.lib import yaml_lite


def test_scalars():
    assert yaml_lite.loads("x: 1") == {"x": 1}
    assert yaml_lite.loads("x: -3.14") == {"x": -3.14}
    assert yaml_lite.loads("x: true") == {"x": True}
    assert yaml_lite.loads("x: false") == {"x": False}
    assert yaml_lite.loads("x: null") == {"x": None}
    assert yaml_lite.loads("x: ~") == {"x": None}
    assert yaml_lite.loads("x:") == {"x": None}
    assert yaml_lite.loads("x: hello") == {"x": "hello"}
    assert yaml_lite.loads('x: "hello world"') == {"x": "hello world"}
    assert yaml_lite.loads("x: 'it''s'") == {"x": "it's"}


def test_nested_mapping():
    text = """
a:
  b:
    c: 1
    d: two
  e: 3
"""
    assert yaml_lite.loads(text) == {"a": {"b": {"c": 1, "d": "two"}, "e": 3}}


def test_inline_list():
    assert yaml_lite.loads("x: []") == {"x": []}
    assert yaml_lite.loads("x: [a, b, c]") == {"x": ["a", "b", "c"]}
    assert yaml_lite.loads('x: [1, 2, "three"]') == {"x": [1, 2, "three"]}


def test_block_list():
    text = """
items:
  - alpha
  - beta
  - 3
"""
    assert yaml_lite.loads(text) == {"items": ["alpha", "beta", 3]}


def test_block_scalar():
    text = """
cmd: |
  cd /tmp && \\
    echo hi
other: 1
"""
    parsed = yaml_lite.loads(text)
    assert parsed["cmd"] == "cd /tmp && \\\n  echo hi\n"
    assert parsed["other"] == 1


def test_list_of_dicts():
    text = """
attempts:
  - id: a
    rc: 0
  - id: b
    rc: 1
"""
    assert yaml_lite.loads(text) == {"attempts": [{"id": "a", "rc": 0}, {"id": "b", "rc": 1}]}


def test_comments():
    text = """
# top-level comment
x: 1  # trailing
y: 2
"""
    assert yaml_lite.loads(text) == {"x": 1, "y": 2}


def test_roundtrip_task_like():
    obj = {
        "id": "pt100-snap005",
        "priority": 100,
        "command": "cd /tmp && echo hi\necho done\n",
        "env": {"SNAP_DIR": "/scratch/x", "CORES": 64},
        "resources": {"cores": 64, "gpus": 0, "memory_gb": 30, "walltime_seconds": 86400},
        "depends_on": [],
        "retry": {"max_attempts": 2, "retry_on_priors": ["x", "y"]},
        "attempts": [],
    }
    text = yaml_lite.dumps(obj)
    back = yaml_lite.loads(text)
    assert back == obj


def test_roundtrip_with_attempts():
    obj = {
        "id": "t1",
        "attempts": [
            {"started_at": "2026-05-26T12:00:00Z", "exit_code": 0, "host": "c1"},
            {"started_at": "2026-05-26T13:00:00Z", "exit_code": 1, "host": "c2"},
        ],
    }
    text = yaml_lite.dumps(obj)
    back = yaml_lite.loads(text)
    assert back == obj


def test_multiline_no_trailing_newline_roundtrips():
    """A multi-line string without a trailing newline must NOT gain one."""
    obj = {"command": "a\nb"}
    text = yaml_lite.dumps(obj)
    assert "|-" in text  # strip-chomp header
    assert yaml_lite.loads(text) == obj


def test_multiline_with_trailing_newline_roundtrips():
    """A multi-line string with one trailing newline must keep exactly one."""
    obj = {"command": "a\nb\n"}
    text = yaml_lite.dumps(obj)
    back = yaml_lite.loads(text)
    assert back == obj


def test_task_multiline_command_no_newline_roundtrips():
    """A Task whose command is multi-line without a trailing newline survives
    a full to_yaml / from_yaml round-trip unchanged."""
    from subjob.lib.task import Task

    t = Task(id="ml", command="cd /tmp && \\\n  echo hi")
    back = Task.from_yaml(t.to_yaml())
    assert back.command == "cd /tmp && \\\n  echo hi"


def test_blank_line_inside_block_scalar_roundtrips():
    """A blank line *inside* a literal block scalar must survive (no compaction)."""
    obj = {"x": "a\n\nb"}
    text = yaml_lite.dumps(obj)
    assert yaml_lite.loads(text) == obj


def test_blank_line_inside_block_scalar_with_trailing_newline_roundtrips():
    obj = {"x": "a\n\nb\n"}
    text = yaml_lite.dumps(obj)
    assert yaml_lite.loads(text) == obj


def test_task_command_with_blank_line_roundtrips():
    """A Task whose command contains a blank line round-trips exactly."""
    from subjob.lib.task import Task

    t = Task(id="ml", command="line one\n\nline three")
    back = Task.from_yaml(t.to_yaml())
    assert back.command == "line one\n\nline three"


def test_blank_line_between_top_level_keys_still_ignored():
    """A blank line between top-level mapping keys is NOT block content."""
    text = "x: 1\n\ny: 2\n"
    assert yaml_lite.loads(text) == {"x": 1, "y": 2}


def test_block_scalar_blank_line_in_list_of_dicts_roundtrips():
    """Blank-line preservation also works for a block scalar nested in a
    list-of-dicts entry (the synthetic-line path)."""
    obj = {"attempts": [{"id": "a", "error": "boom\n\ntrace"}]}
    text = yaml_lite.dumps(obj)
    assert yaml_lite.loads(text) == obj


def test_rejects_tabs():
    with pytest.raises(yaml_lite.ParseError, match="tab"):
        yaml_lite.loads("a:\n\tb: 1")


def test_rejects_anchors():
    with pytest.raises(yaml_lite.ParseError):
        yaml_lite.loads("a: &anchor 1")


def test_rejects_multidoc():
    with pytest.raises(yaml_lite.ParseError):
        yaml_lite.loads("---\na: 1\n---\nb: 2")


def test_rejects_flow_map_with_content():
    with pytest.raises(yaml_lite.ParseError):
        yaml_lite.loads("x: {a: 1}")


def test_double_quoted_unknown_escape_preserved():
    """Unknown backslash escapes in double-quoted scalars must pass through
    with the backslash intact — priors authors write "\\d+" expecting "\\d+".
    """
    # "\d+" in YAML source → r"\d+" in Python (3 chars: \, d, +).
    result = yaml_lite.loads(r'x: "\d+"')
    assert result == {"x": r"\d+"}
    assert len(result["x"]) == 3


def test_double_quoted_known_escapes_still_work():
    """Known C-style escapes still decode."""
    result = yaml_lite.loads(r'x: "a\nb"')
    assert result == {"x": "a\nb"}
    assert len(result["x"]) == 3
    assert yaml_lite.loads(r'x: "a\tb"') == {"x": "a\tb"}
    assert yaml_lite.loads(r'x: "a\\b"') == {"x": "a\\b"}
    assert yaml_lite.loads(r'x: "a\"b"') == {"x": 'a"b'}
