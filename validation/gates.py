"""Pass/fail gate predicates for the validation harness.

A `Gate` is a callable that takes a `Pool` and returns a `GateResult`. The
runner collects results and writes them to a markdown report. Gates compose
trivially via lists.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from subjob.lib.pool import Pool
from subjob.lib.task import Task


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str


Gate = Callable[[Pool], GateResult]


# ----- status / placement -----


def status_equals(expected: dict[str, int]) -> Gate:
    def gate(pool: Pool) -> GateResult:
        actual = pool.status()
        ok = actual == expected
        return GateResult("status_equals", ok, f"expected {expected}, got {actual}")

    return gate


def status_done_failed(done: int, failed: int) -> Gate:
    def gate(pool: Pool) -> GateResult:
        s = pool.status()
        ok = s["done"] == done and s["failed"] == failed and s["pending"] == 0 and s["claimed"] == 0
        return GateResult("status_done_failed", ok, f"expected done={done} failed={failed}, got {s}")

    return gate


def all_in_state(state: str, expected_ids: set[str]) -> Gate:
    def gate(pool: Pool) -> GateResult:
        actual = {p.stem for p in pool.list_state(state)}
        missing = expected_ids - actual
        extra = actual - expected_ids
        ok = not missing and not extra
        if ok:
            detail = f"{len(actual)} tasks in {state}/"
        else:
            detail = f"missing={sorted(missing)[:5]} extra={sorted(extra)[:5]}"
        return GateResult(f"all_in_state({state})", ok, detail)

    return gate


# ----- claim invariants -----


def no_double_claims() -> Gate:
    def gate(pool: Pool) -> GateResult:
        claims: Counter[str] = Counter()
        for ev in pool.read_journal():
            if ev["type"] == "task_claimed":
                claims[ev["task_id"]] += 1
        doubles = {k: v for k, v in claims.items() if v > 1}
        ok = not doubles
        detail = f"{len(claims)} unique claims" if ok else f"DOUBLES: {dict(list(doubles.items())[:5])}"
        return GateResult("no_double_claims", ok, detail)

    return gate


# ----- task-level inspection -----


def task_attempt_field(task_id: str, key: str, expected: Any, state: str = "*") -> Gate:
    def gate(pool: Pool) -> GateResult:
        states = ("done", "failed") if state == "*" else (state,)
        for st in states:
            path = pool.root / st / f"{task_id}.yaml"
            if path.exists():
                t = Task.read(path)
                if not t.attempts:
                    return GateResult(f"attempt_field({task_id},{key})", False, "no attempts")
                actual = t.attempts[-1].get(key)
                ok = actual == expected
                return GateResult(
                    f"attempt_field({task_id},{key})",
                    ok,
                    f"expected {expected!r}, got {actual!r} (in {st}/)",
                )
        return GateResult(f"attempt_field({task_id},{key})", False, f"task not found in {states}")

    return gate


def task_duration_within(task_id: str, lo: float, hi: float, state: str = "*") -> Gate:
    def gate(pool: Pool) -> GateResult:
        states = ("done", "failed") if state == "*" else (state,)
        for st in states:
            path = pool.root / st / f"{task_id}.yaml"
            if path.exists():
                t = Task.read(path)
                if not t.attempts:
                    return GateResult(f"duration({task_id})", False, "no attempts")
                dur = t.attempts[-1].get("duration_s", -1.0)
                ok = lo <= dur <= hi
                return GateResult(
                    f"duration({task_id})",
                    ok,
                    f"duration={dur:.3f}s, range=[{lo},{hi}]",
                )
        return GateResult(f"duration({task_id})", False, "task not found")

    return gate


def task_stdout_contains(task_id: str, substring: str) -> Gate:
    def gate(pool: Pool) -> GateResult:
        for st in ("done", "failed"):
            path = pool.root / st / f"{task_id}.yaml"
            if path.exists():
                t = Task.read(path)
                if t.attempts:
                    tail = t.attempts[-1].get("stdout_tail", "")
                    if substring in tail:
                        return GateResult(
                            f"stdout_contains({task_id})",
                            True,
                            f"found in {st}/ stdout_tail",
                        )
                    return GateResult(
                        f"stdout_contains({task_id})",
                        False,
                        f"substring {substring!r} not in stdout_tail (first 200 chars: {tail[:200]!r})",
                    )
        return GateResult(f"stdout_contains({task_id})", False, "task not found")

    return gate


# ----- journal inspection -----


def journal_event_present(event_type: str, task_id: str | None = None) -> Gate:
    def gate(pool: Pool) -> GateResult:
        for ev in pool.read_journal():
            if ev["type"] == event_type and (task_id is None or ev["task_id"] == task_id):
                return GateResult(
                    f"journal_event({event_type},{task_id})",
                    True,
                    f"found at {ev['timestamp']}",
                )
        return GateResult(f"journal_event({event_type},{task_id})", False, "not in journal")

    return gate


def journal_event_count(event_type: str, expected: int) -> Gate:
    def gate(pool: Pool) -> GateResult:
        n = sum(1 for ev in pool.read_journal() if ev["type"] == event_type)
        ok = n == expected
        return GateResult(f"journal_event_count({event_type})", ok, f"expected {expected}, got {n}")

    return gate


# ----- performance -----


def throughput_at_least(min_rate: float) -> Gate:
    def gate(pool: Pool) -> GateResult:
        events = pool.read_journal()
        starts = [e for e in events if e["type"] == "worker_started"]
        dones = [e for e in events if e["type"] == "task_done"]
        if not starts or not dones:
            return GateResult("throughput", False, "missing worker_started or task_done events")
        first = starts[0]["event_id"]
        last = max(e["event_id"] for e in dones)
        span_s = (last - first) / 1e9
        rate = len(dones) / span_s if span_s > 0 else float("inf")
        ok = rate >= min_rate
        return GateResult(
            "throughput",
            ok,
            f"{rate:.2f} tasks/s {'>= ' if ok else '< '}{min_rate} ({len(dones)} tasks in {span_s:.1f}s)",
        )

    return gate


def concurrent_at_peak(min_concurrent: int) -> Gate:
    def gate(pool: Pool) -> GateResult:
        events = pool.read_journal()
        starts: dict[str, int] = {}
        intervals: list[tuple[int, int]] = []
        for ev in events:
            if ev["type"] == "task_started":
                starts[ev["task_id"]] = ev["event_id"]
            elif ev["type"] in ("task_done", "task_failed") and ev["task_id"] in starts:
                intervals.append((starts.pop(ev["task_id"]), ev["event_id"]))
        if not intervals:
            return GateResult("concurrent_at_peak", False, "no task intervals")
        # Sweep-line peak count.
        edges = sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals])
        peak = cur = 0
        for _, delta in edges:
            cur += delta
            peak = max(peak, cur)
        ok = peak >= min_concurrent
        return GateResult(
            "concurrent_at_peak",
            ok,
            f"peak={peak}, required>={min_concurrent}",
        )

    return gate
