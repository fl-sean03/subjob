# subjob

Run many tasks inside one HPC allocation. Designed for agents + humans submitting heterogeneous compute work to SLURM clusters (and eventually cloud).

> **Status:** Phase 0 MVP shipped 2026-05-26. Pool + worker + CLI + SLURM backend are in place and tested; Phase 1 dogfood begins when the hydrogenation cool+prod fan-out completes.
>
> **👉 If you are an agent picking this up, read [START_HERE.md](./START_HERE.md) first.** It is the canonical onboarding doc.

## Quickstart

```bash
# Install (stdlib-only runtime; pytest + ruff for dev)
pip install -e .

# Run the end-to-end demo: 5 sleep tasks, one in-process worker, all land in done/
python examples/sleep_test.py

# Or use the CLI against your own pool:
python -m subjob.client.cli submit --pool /tmp/mypool --task-file my-task.yaml
python -m subjob.client.cli status --pool /tmp/mypool
python -m subjob.worker --pool /tmp/mypool --cores 4 --idle-timeout 5
python -m subjob.client.cli follow --pool /tmp/mypool --timeout 10
```

To preview the Phase 1 dogfood workload (60 Pt{100,111,110}-snap-* analysis tasks) without submitting it, run `python examples/per_snapshot_analysis_dryrun.py`.

## Why this exists

Submitting 100 short tasks as 100 separate `sbatch` jobs means waiting in the SLURM queue 100 times. `subjob` lets you submit one big allocation that internally cycles through tasks as cores free. One queue wait, then continuous work.

Inspired by [fl-sean03/allocation-scheduler](https://github.com/fl-sean03/allocation-scheduler) and built up to lab-grade.

**Available now (Phase 0 / 0.5 + Cycle 2 infra pull-forward 2026-05-30):**

- Multi-pilot (multiple workers pull from one shared task pool — across nodes / partitions / clusters)
- Event journaling (tailable JSONL for agents)
- Agent-first API (Python module + structured JSON, not "edit a file")
- Clean preemption (claims released + retried; forked children reaped)
- Per-task walltime enforcement + release-retry cap
- **Automatic dead-worker recovery via heartbeats** — live workers detect a dead cohort-mate via missing/stale `<pool>/.heartbeats/<worker_id>` and release its claims. `subjob reap-stale --auto` remains as the operator escape hatch.
- **Task DAG / `depends_on` enforcement** — workers refuse to dispatch a dependent until all deps are in `done/`; failed deps cascade, unknown deps fail-fast after a one-cycle grace
- **Artifact validation** — declared `artifacts.expect` + `success_marker` checked after exit 0; a "successful" task that didn't produce its outputs is recorded as a real failure
- **Failure intelligence: priors framework + `pool.diagnose` / `subjob diagnose`** — per-pool `priors.yaml` catalog, advisory verdicts + suggested fixes against failed tasks
- Basic resource gating (cores/GPUs as a capacity counter)

**Planned (not yet implemented — see `docs/AGENT_GUIDE.md` "not yet implemented"):**

- `pool.read_artifact` (validated artifact read-back helper — validation itself is shipped; the one-call read+verify helper is still future) — *Phase 1*
- Auto-applied priors mitigations (worker honors `priors_apply` automatically; today they're parsed but inert) — *Phase 1+*
- Full GPU resource accounting (multi-GPU type / per-device counting) — *Phase 2*
- Cross-platform cloud backend (CCM / Vast.ai) — *Phase 2*
- Cost-aware backend selection — *Phase 2*

## Quick map

| File | What |
|---|---|
| `README.md` | This file |
| `docs/ARCHITECTURE.md` | The full design decision record |
| `docs/AGENT_GUIDE.md` | How agents use subjob (skill-friendly) |
| `docs/DEVELOPMENT.md` | Phase plan (dogfood-driven, not speculative) |
| `docs/DEPLOYMENT.md` | SLURM / CCM / local backends + operations |
| `src/subjob/` | The code (Python stdlib only, like the upstream) |
| `examples/` | Concrete use cases |

## Where it sits relative to existing lab tooling

| Tool | Scope | Status |
|---|---|---|
| `46-CCM` (CloudComputeManager) | Vast.ai job orchestration (single-job, single-instance) | Existing — under cross-analysis to decide if it folds into subjob or stays separate |
| `subjob` (this) | Multi-task pilot scheduler across SLURM + (eventually) cloud | New — Phase 0 |

See `docs/CCM_INTEGRATION_ANALYSIS.md` for the cross-analysis.
