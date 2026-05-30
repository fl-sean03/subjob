"""Priors — per-pool catalog of known failure patterns.

A `priors.yaml` file at `<pool>/priors.yaml` is an optional catalog of known
failure modes the operator has classified before. Each entry has:

  * ``id``: a short stable identifier (required, non-empty string).
  * ``description``: human-readable summary (optional).
  * ``match``: optional dict of conditions that ALL must hold for the prior
    to match a failed task:
      - ``exit_code``: int — must equal the failed task's exit code
      - ``stderr_regex``: str — ``re.search()`` must find a match in
        the failed task's stderr tail
      - ``walltime_killed``: bool — must equal the failed task's flag
    An ABSENT ``match`` block (or an empty one) is a catch-all that
    matches every failure (useful for an "unknown failure: please review"
    fallback entry at the end of the list).
  * ``verdict``: human-friendly classification (required string).
  * ``suggested_fix``: human-readable remediation (optional).
  * ``auto_apply``: parsed but NOT honored in Phase 1. The framework lays
    the schema; the worker does not act on it.

This module is intentionally a pure-function library: it knows about Tasks
or Pools nothing — only the failure signal dict shape. ``Pool.diagnose`` is
the thin caller that gathers the signal and invokes ``match_priors``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from subjob.lib import yaml_lite


class PriorSchemaError(ValueError):
    """Raised when a priors.yaml entry has a malformed schema."""


_VALID_MATCH_KEYS = {"exit_code", "stderr_regex", "walltime_killed"}


@dataclass
class Prior:
    """A single catalog entry. The compiled stderr regex is cached lazily."""

    id: str
    description: str = ""
    match: dict[str, Any] = field(default_factory=dict)
    verdict: str = ""
    suggested_fix: str = ""
    auto_apply: bool = False
    _stderr_re: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Eager-compile the regex at construction time so a bad pattern in
        # priors.yaml surfaces at load_priors() rather than at first match.
        pattern = self.match.get("stderr_regex") if isinstance(self.match, dict) else None
        if pattern is not None:
            if not isinstance(pattern, str):
                raise PriorSchemaError(
                    f"prior {self.id!r}: match.stderr_regex must be a string, "
                    f"got {type(pattern).__name__}"
                )
            try:
                self._stderr_re = re.compile(pattern)
            except re.error as e:
                raise PriorSchemaError(
                    f"prior {self.id!r}: invalid stderr_regex {pattern!r}: {e}"
                ) from e

    def to_dict(self) -> dict[str, Any]:
        """Serializable form (the cached regex is dropped)."""
        return {
            "id": self.id,
            "description": self.description,
            "match": dict(self.match),
            "verdict": self.verdict,
            "suggested_fix": self.suggested_fix,
            "auto_apply": self.auto_apply,
        }


def load_priors(path: Path) -> list[Prior]:
    """Load a priors.yaml file. Returns [] if missing.

    Raises ``PriorSchemaError`` if the file exists but any entry is malformed
    (missing ``id``, non-string ``verdict``, unknown ``match`` key, bad
    regex, etc.). Empty / null top-level priors list yields ``[]``.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except OSError as e:
        raise PriorSchemaError(f"could not read {path}: {e}") from e
    try:
        data = yaml_lite.loads(text)
    except yaml_lite.ParseError as e:
        raise PriorSchemaError(f"could not parse {path}: {e}") from e

    if data is None:
        return []
    if not isinstance(data, dict):
        raise PriorSchemaError(
            f"{path}: top-level must be a mapping with a 'priors' key, "
            f"got {type(data).__name__}"
        )
    raw_priors = data.get("priors")
    if raw_priors is None:
        return []
    if not isinstance(raw_priors, list):
        raise PriorSchemaError(
            f"{path}: 'priors' must be a list, got {type(raw_priors).__name__}"
        )

    out: list[Prior] = []
    seen_ids: set[str] = set()
    for idx, entry in enumerate(raw_priors):
        if not isinstance(entry, dict):
            raise PriorSchemaError(
                f"{path}: priors[{idx}] must be a mapping, got {type(entry).__name__}"
            )
        pid = entry.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise PriorSchemaError(
                f"{path}: priors[{idx}].id must be a non-empty string"
            )
        if pid in seen_ids:
            raise PriorSchemaError(f"{path}: duplicate prior id {pid!r}")
        seen_ids.add(pid)
        verdict = entry.get("verdict", "")
        if not isinstance(verdict, str):
            raise PriorSchemaError(
                f"{path}: prior {pid!r}: verdict must be a string"
            )
        description = entry.get("description") or ""
        if not isinstance(description, str):
            raise PriorSchemaError(
                f"{path}: prior {pid!r}: description must be a string"
            )
        suggested_fix = entry.get("suggested_fix") or ""
        if not isinstance(suggested_fix, str):
            raise PriorSchemaError(
                f"{path}: prior {pid!r}: suggested_fix must be a string"
            )
        match = entry.get("match") or {}
        if not isinstance(match, dict):
            raise PriorSchemaError(
                f"{path}: prior {pid!r}: match must be a mapping"
            )
        unknown = set(match) - _VALID_MATCH_KEYS
        if unknown:
            raise PriorSchemaError(
                f"{path}: prior {pid!r}: unknown match keys {sorted(unknown)!r} "
                f"(allowed: {sorted(_VALID_MATCH_KEYS)!r})"
            )
        if "exit_code" in match and not isinstance(match["exit_code"], int):
            raise PriorSchemaError(
                f"{path}: prior {pid!r}: match.exit_code must be an int"
            )
        if "walltime_killed" in match and not isinstance(match["walltime_killed"], bool):
            raise PriorSchemaError(
                f"{path}: prior {pid!r}: match.walltime_killed must be bool"
            )
        auto_apply = entry.get("auto_apply", False)
        if not isinstance(auto_apply, bool):
            raise PriorSchemaError(
                f"{path}: prior {pid!r}: auto_apply must be bool"
            )
        out.append(
            Prior(
                id=pid,
                description=description,
                match=match,
                verdict=verdict,
                suggested_fix=suggested_fix,
                auto_apply=auto_apply,
            )
        )
    return out


def match_priors(
    priors: list[Prior],
    *,
    exit_code: int | None,
    walltime_killed: bool | None,
    stderr_tail: str,
) -> list[Prior]:
    """Return priors whose ``match`` block matches the failure signal.

    For each condition in ``match``:
      - ``exit_code``: if specified, must equal the observed ``exit_code``.
        If the observed value is None, a match block that specifies
        ``exit_code`` cannot match (we have no value to compare).
      - ``walltime_killed``: if specified, must equal the observed flag.
        If the observed value is None, the constraint cannot match.
      - ``stderr_regex``: ``re.search()`` over ``stderr_tail``.

    A prior with an empty ``match`` block matches every failure (catch-all).
    Order in the input list is preserved in the output list.
    """
    out: list[Prior] = []
    for prior in priors:
        m = prior.match
        if "exit_code" in m:
            if exit_code is None or exit_code != m["exit_code"]:
                continue
        if "walltime_killed" in m:
            if walltime_killed is None or walltime_killed != m["walltime_killed"]:
                continue
        if prior._stderr_re is not None:
            if not prior._stderr_re.search(stderr_tail):
                continue
        out.append(prior)
    return out
