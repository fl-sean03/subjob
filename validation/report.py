"""Markdown report writer for tier runs."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from validation.gates import GateResult


@dataclass
class TierReport:
    tier_name: str
    pool_path: str
    backend: str
    worker_summary: str
    duration_s: float
    gates: list[GateResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def write(self, path: Path) -> None:
        lines = [
            f"# {self.tier_name} report — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            "",
            f"- **Pool:** `{self.pool_path}`",
            f"- **Backend:** {self.backend}",
            f"- **Workers:** {self.worker_summary}",
            f"- **Duration:** {self.duration_s:.1f}s",
            "",
            "## Gates",
            "",
        ]
        passed = sum(1 for g in self.gates if g.passed)
        for g in self.gates:
            mark = "PASS" if g.passed else "FAIL"
            lines.append(f"- [{mark}] **{g.name}** — {g.detail}")
        lines += ["", f"**{passed}/{len(self.gates)} gates passed.**", ""]
        if self.notes:
            lines += ["## Notes", "", *(f"- {n}" for n in self.notes), ""]
        path.write_text("\n".join(lines))

    def summary(self) -> str:
        passed = sum(1 for g in self.gates if g.passed)
        return f"{self.tier_name}: {passed}/{len(self.gates)} gates passed ({'PASS' if self.passed else 'FAIL'})"
