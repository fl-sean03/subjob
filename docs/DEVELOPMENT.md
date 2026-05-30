# Development plan — subjob

Dogfood-driven. Each phase delivers something used in production before the next phase starts.

## Phase 0 — MVP (target: ~2-3 days of focused work)

**Goal**: minimum viable pool+worker+CLI that can run our per-snapshot analysis pipeline.

**Scope**:
- Pool class (pending/claimed/done/failed dirs, atomic claim via rename)
- Worker that polls + claims + runs + journals
- `subjob submit` (write task YAML)
- `subjob status` (count files per state dir)
- `subjob follow` (tail -f journal.jsonl with parsing)
- 1 backend: `slurm` (worker started by an sbatch wrapper)
- 5-line journal: timestamp, task_id, event_type, payload
- Sanity tests against fl-sean03's LAMMPS example

**Out of scope for Phase 0**:
- Task dependencies (`depends_on`)
- Priors integration
- Artifact validation
- GPU resource accounting
- Cancellation API
- Multi-pilot heartbeats

**Validation**:
- Submit 5 trivial tasks (sleep + echo), 1 worker, all 5 land in done/
- Submit + kill worker mid-task → restart worker → task gets reclaimed + completes
- Submit fl-sean03 LAMMPS parameter sweep → matches their pilot's output

## Phase 1 — Dogfood against per-snapshot analysis

**Phase 0 shipped 2026-05-26.** Pool API, worker (concurrent tasks + walltime
awareness), CLI (submit/status/follow/cancel), SLURM backend, examples
(sleep_test + per_snapshot_analysis_dryrun), and a stdlib-only YAML subset
parser are all in place — 63 passing tests, ruff clean. Phase 1 begins
when the cool+prod fan-out completes on `~/LabWork/Workspace/31-Hydrogenation/`.

**Trigger**: cool+prod fan-out finishes (60 simulation.dcd files exist).

**Dogfood workload**: `/hydrog-run-pipeline` per snapshot × 60 = 60 analysis tasks (2-15 min each). This is the highest-leverage pilot case in the project.

**Features added only as the workload demanded them**:
- Whatever pain points emerge from Phase 0 use
- Likely: artifact validation (was the analysis output actually written?)
- Likely: failure classification (cross-ref priors.yaml when a task fails)
- Likely: better journal event types for agent consumption

**Validation**: 60 snapshots analyzed, results comparable to running them manually one-at-a-time.

## Phase 2 — DAG + cross-platform

**Trigger**: a real workload needs both. Candidates:
- Force-field parameter sweep (build → MD → analyze per param)
- Y.11-Y.13 follow-up sweep
- MXene Stage-1 continuation

**Features**:
- `depends_on` enforced by worker (task not claimable until deps done)
- Backend abstraction: support both slurm + CCM
- CCM integration: workers can run on Vast.ai too, pulling from same pool
- Cost-aware backend selection (cheapest backend that meets resource constraints)

## Phase 3 — Lab-wide rollout

**Trigger**: 3+ projects asking for it.

**Features**:
- TUI dashboard (rich) for human users
- Skill polish + agent guide refinement
- Documentation aimed at "next user" not "designer"
- Migration guide for projects switching from individual sbatch / CCM CLI

## Anti-features (do NOT build until forced)

| Tempting feature | Why we resist |
|---|---|
| Web UI | TUI is 10% the work, 90% the value |
| Database backing | Filesystem state survives node failure; database adds dependency hell |
| Auth/multi-tenant | Lab is single-trust; add when external collab asks |
| Workflow DSL | YAML + depends_on is enough; DSL is a tarpit |
| Auto-scaling | Workers are sbatch jobs; user controls count |
| Cluster federation | Cross-cluster is a year-long project; not worth the cost |

## Cadence

- Phase 0 → 2-3 focused days of work
- Phase 1 → 1-2 weeks of dogfood + iteration
- Phase 2 → 4-6 weeks
- Phase 3 → ongoing

Stay phase 0-2 unless 3 separate projects ask for phase 3.
