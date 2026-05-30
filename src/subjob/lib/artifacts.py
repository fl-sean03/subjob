"""Artifact validation — check a Task's declared outputs after the command runs.

A task spec may declare two kinds of artifacts under ``artifacts``:

  * ``expect``: a list of paths that MUST exist after the command finishes.
  * ``success_marker``: a ``{file, contains}`` pair — the file must exist AND
    its contents must contain the given substring.

This module is intentionally a pure-function library: it does not know about
the pool, the worker, or the journal. The worker calls ``validate_artifacts``
after a task exits 0 and uses the returned ``(ok, detail)`` to decide whether
to commit ``done`` or ``failed``.

Path resolution:
  * ``$VAR`` / ``${VAR}`` are expanded against the task's ``env`` dict (NOT
    against the calling process's environ — the task's env is authoritative).
  * ``~`` is expanded against ``HOME`` from that same env, falling back to the
    calling process's HOME.
  * Relative paths are resolved against ``workdir`` if supplied, else
    ``os.getcwd()``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# Match $VAR and ${VAR}. The braced form lets users disambiguate (e.g.
# "${SNAP_DIR}_extra"). $$ is treated as a literal $ to match
# os.path.expandvars semantics.
_VAR_PATTERN = re.compile(r"\$(\$)|\$(\w+)|\$\{([^}]+)\}")


def validate_artifacts(
    artifacts: dict[str, Any] | None,
    env: dict[str, str] | None,
    workdir: str | None,
) -> tuple[bool, dict[str, Any]]:
    """Validate a Task's declared artifacts.

    Returns ``(ok, detail)``. ``detail`` is a structured dict suitable to
    attach to the task attempt (and journal payload). It contains fields like
    ``missing_expect``, ``success_marker_path``, ``success_marker_found``.

    An empty or missing ``artifacts`` dict yields ``(True, {"artifacts": "none declared"})``.
    """
    if not artifacts:
        return True, {"artifacts": "none declared"}

    env = dict(env or {})
    base = workdir if workdir else os.getcwd()
    base = _expand(base, env)

    detail: dict[str, Any] = {}
    ok = True

    expect = artifacts.get("expect") or []
    if expect:
        missing: list[str] = []
        checked: list[str] = []
        for raw in expect:
            resolved = _resolve(str(raw), env, base)
            checked.append(resolved)
            if not Path(resolved).exists():
                missing.append(resolved)
        detail["expect_checked"] = checked
        if missing:
            ok = False
            detail["missing_expect"] = missing

    marker = artifacts.get("success_marker")
    if marker:
        marker_file = marker.get("file") if isinstance(marker, dict) else None
        marker_contains = marker.get("contains") if isinstance(marker, dict) else None
        if not marker_file:
            ok = False
            detail["success_marker_error"] = "missing 'file' in success_marker"
        else:
            resolved = _resolve(str(marker_file), env, base)
            detail["success_marker_path"] = resolved
            p = Path(resolved)
            if not p.exists():
                ok = False
                detail["success_marker_missing"] = True
                detail["success_marker_found"] = False
            else:
                if marker_contains is None:
                    # File-only marker: existence is enough.
                    detail["success_marker_found"] = True
                else:
                    needle = str(marker_contains)
                    try:
                        text = p.read_text(errors="replace")
                    except OSError as e:
                        ok = False
                        detail["success_marker_error"] = f"read failed: {e}"
                        detail["success_marker_found"] = False
                    else:
                        found = needle in text
                        detail["success_marker_contains"] = needle
                        detail["success_marker_found"] = found
                        if not found:
                            ok = False

    return ok, detail


def _expand(s: str, env: dict[str, str]) -> str:
    """Expand $VAR / ${VAR} from ``env``, then ``~`` from env's HOME.

    Unknown variables are left as-is (mirroring ``os.path.expandvars``).

    Task env is the ONLY source — we do NOT fall back to ``os.environ``.
    This matches the documented contract (module docstring + AGENT_GUIDE
    "Artifact validation" section): a missing ``${VAR}`` is a real artifact
    validation failure (the resulting path won't exist), not a silent
    success against a worker-process env var the task author didn't intend.
    """

    def _sub(m: re.Match[str]) -> str:
        if m.group(1):  # "$$" → literal "$"
            return "$"
        name = m.group(2) or m.group(3)
        if name in env:
            return env[name]
        return m.group(0)

    expanded = _VAR_PATTERN.sub(_sub, s)
    if expanded.startswith("~"):
        home = env.get("HOME") or os.path.expanduser("~")
        if expanded == "~":
            expanded = home
        elif expanded.startswith("~/"):
            expanded = home + expanded[1:]
        # Bare "~user" forms fall through to expanduser for posix-style handling.
        else:
            expanded = os.path.expanduser(expanded)
    return expanded


def _resolve(path: str, env: dict[str, str], base: str) -> str:
    """Expand env vars and ``~`` in ``path``, then resolve relative paths against ``base``."""
    expanded = _expand(path, env)
    if not os.path.isabs(expanded):
        expanded = os.path.join(base, expanded)
    return os.path.normpath(expanded)
