# subjob

Run many tasks inside one HPC allocation. Designed for agents + humans submitting heterogeneous compute work to SLURM clusters (and eventually cloud).

> **Status:** Phase 0 design phase, 2026-05-25. No code yet. See `docs/ARCHITECTURE.md` and `docs/DEVELOPMENT.md`.

## Why this exists

Submitting 100 short tasks as 100 separate `sbatch` jobs means waiting in the SLURM queue 100 times. `subjob` lets you submit one big allocation that internally cycles through tasks as cores free. One queue wait, then continuous work.

Inspired by [fl-sean03/allocation-scheduler](https://github.com/fl-sean03/allocation-scheduler) but built up to lab-grade with:

- Multi-pilot (multiple workers pull from one shared task pool — across nodes / partitions / clusters)
- GPU resource accounting
- Task DAG (depends_on)
- Failure intelligence (priors-based mitigation)
- Artifact validation (declared expects + success markers)
- Event journaling (tailable JSONL for agents)
- Cross-platform (SLURM today, CCM/Vast.ai later)
- Agent-first API (Python module + structured JSON, not "edit a file")

## Quick map

| File | What |
|---|---|
| `README.md` | This file |
| `docs/ARCHITECTURE.md` | The full design decision record |
| `docs/AGENT_GUIDE.md` | How agents use subjob (skill-friendly) |
| `docs/DEVELOPMENT.md` | Phase plan (dogfood-driven, not speculative) |
| `docs/BACKENDS.md` | SLURM / CCM / local execution backends |
| `src/subjob/` | The code (Python stdlib only, like the upstream) |
| `examples/` | Concrete use cases |
| `.claude/skills/subjob-submit/` | Agent skill for submission |

## Where it sits relative to existing lab tooling

| Tool | Scope | Status |
|---|---|---|
| `46-CCM` (CloudComputeManager) | Vast.ai job orchestration (single-job, single-instance) | Existing — under cross-analysis to decide if it folds into subjob or stays separate |
| `subjob` (this) | Multi-task pilot scheduler across SLURM + (eventually) cloud | New — Phase 0 |

See `docs/CCM_INTEGRATION_ANALYSIS.md` for the cross-analysis.
