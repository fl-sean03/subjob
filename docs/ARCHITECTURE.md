# Architecture — subjob

> Captured 2026-05-25 from design discussion. Don't change without an updated decision record.

## Core concept

Submit one big SLURM allocation. Inside it, a **worker** process pulls **tasks** from a **shared pool directory** on `/scratch`. Multiple workers can pull from the same pool concurrently (different SLURM allocations, different partitions, different clusters — all sharing the task pool via the filesystem). Tasks are claimed atomically via filesystem rename.

```
┌────────────────────────────────────────────────────────────────────┐
│  Pool directory (/scratch/.../my-pool/)                             │
│                                                                     │
│   pending/         claimed/         done/         failed/           │
│   ├─ t001.yaml     ├─ t002.yaml     ├─ t000.yaml   ├─ t009.yaml     │
│   ├─ t003.yaml                                                       │
│                                                                     │
│   journal.jsonl   ← append-only event stream                         │
│   pool.lock       ← optional cross-claim coordination                │
└────────────────────────────────────────────────────────────────────┘
       ▲                              ▲                        ▲
       │                              │                        │
       │ submit                       │ claim/run/release      │ follow
       │                              │                        │
   ┌───┴────┐                ┌────────┴───────┐         ┌──────┴─────┐
   │ Agent  │                │ Worker (pilot) │         │  Watcher   │
   │ Human  │                │ in SLURM alloc │         │  (agent)   │
   │ CLI    │                └────────────────┘         └────────────┘
   └────────┘                       │                          │
                                    │                          │
                              ┌─────┴─────┐              ┌─────┴─────┐
                              │ amilan    │              │ Status,   │
                              │ c128 7d   │              │ events,   │
                              └───────────┘              │ diagnose  │
                              ┌───────────┐              └───────────┘
                              │ aa100 GPU │
                              │ 1d        │
                              └───────────┘
                              ┌───────────┐
                              │ Vast.ai   │
                              │ (via CCM) │
                              └───────────┘
```

## Choice rationale

| Decision | Why |
|---|---|
| **Filesystem as broker** | HPC reality — `/scratch` is already shared, atomic rename works, no server to maintain. Death of any worker doesn't lose state. |
| **YAML task files** | Human-readable, no schema migrations, version-controllable, easy for agents to write directly. |
| **JSONL event journal** | Append-only, tail-friendly, parseable. Agents `tail -f` for real-time updates. |
| **Pluggable backends** | Workers don't care which scheduler launched them. Same worker code runs in SLURM/CCM/local. |
| **Python stdlib only** | Same constraint as upstream — works in any HPC environment without conda. |
| **No persistent daemon** | Avoids "do I have permission to run a daemon" politics on login nodes. Worker is just an sbatch process. |

## Task lifecycle

```
submit          claim                run             complete
   │              │                   │                 │
   ▼              ▼                   ▼                 ▼
pending/  →   claimed/        →   running       →   done/    or   failed/
   │              │                   │                 │              │
   │      atomic rename       process spawned       artifact     classification
   │      (mkdir-style lock)  + stdout/err captured  validated    via priors
   │                          + journal entries
```

## Task spec (canonical YAML)

```yaml
# my-pool/pending/pt100-snap005-coolprod.task.yaml
id: pt100-snap005-coolprod
priority: 100  # higher = runs sooner
state: pending  # written by worker as it transitions

# What to run
command: |
  cd $SNAP_DIR && \
    mpirun -n $CORES /curc/sw/install/namd/3.0.1_cpu/.../namd3 \
    cooling_production_453K.namd

env:
  SNAP_DIR: /scratch/alpine/sefl7948/hydrogenation-surfaces/Pt100/jobs/snapshot_005
  CORES: 64

# Resource requirements
resources:
  cores: 64
  gpus: 0
  memory_gb: 30
  walltime_seconds: 86400

# Where it can run
backend_hints:
  prefer_backend: slurm-alpine
  prefer_partition: amilan
  prefer_qos: long
  avoid_nodes: [c3gpu-a9-u17-1]

# What it produces
artifacts:
  expect:
    - $SNAP_DIR/simulation.dcd
    - $SNAP_DIR/simulation.restart.coor
  success_marker:
    file: $SNAP_DIR/namd_cool_prod.log
    contains: "PRODUCTION COMPLETE"

# DAG
depends_on: []  # list of task ids that must be 'done' first

# Retry policy
retry:
  max_attempts: 2
  retry_on_priors: [namd-restart-config-cwd-mismatch]  # auto-retry only when prior says it's safe

# Auto-mitigations
priors_apply:
  - alpine-al40-u17-1-sigrtmin19  # worker adds --exclude when running on al40

# Bookkeeping (written by worker)
attempts: []
created_at: 2026-05-25T22:30:00Z
```

## Worker

```python
# worker.py — runs inside a SLURM allocation (or anywhere)
def main():
    pool = Pool(pool_dir)
    capabilities = detect()  # node hostname, cores, GPUs, partition
    while not walltime_imminent():
        task = pool.claim_next_runnable(capabilities)
        if not task:
            sleep(POLL_INTERVAL); continue
        result = run(task, capabilities)
        pool.commit_result(task, result)
        journal.emit({"type": "task_done", "task_id": task.id, ...})
    pool.release_unfinished_claims(my_host)  # so another worker picks them up
```

Resource matching is dumb-but-correct: task asks for `cores: 64, gpus: 1` → worker checks `capabilities.cores_remaining ≥ 64 and capabilities.gpus_remaining ≥ 1` → if yes, claim + decrement local capability counters.

## Client (agent-facing)

> **Design sketch — keep `docs/AGENT_GUIDE.md` as the source of truth for the
> real API.** The aspirational method names in this sketch were never built
> as written. Their real-API equivalents (all shipped):
>
> - `pool.failed()` → `pool.list_state("failed")` + `pool.read_task("failed", id)`
> - `task.diagnosis()` → `pool.diagnose(task_id)`
> - `subjob diagnose` → shipped (Cycle 2, 2026-05-30)
>
> Still future: `pool.read_artifact(task_id, name)` (a one-call validated
> read-back helper; artifact VALIDATION itself is shipped — see
> AGENT_GUIDE "Artifact validation").

```python
from subjob import Pool, Task

pool = Pool("/scratch/.../my-pool")

# Submit
ids = pool.submit_batch([
    Task(id=f"snap{i:03d}", command=...) for i in range(20)
])

# Status
print(pool.status())  # {pending: 17, claimed: 3, done: 0, failed: 0}  (claimed = in-flight)

# Wait for batch completion
for event in pool.follow(timeout_s=3600):
    if event["type"] == "task_done":
        print(f"  ✓ {event['task_id']}")

# Diagnose
for task in pool.failed():
    print(task.diagnosis())  # cross-references priors.yaml
```

CLI equivalents: `subjob submit`, `subjob status`, `subjob follow`, `subjob diagnose`, `subjob cancel`.

## Backends

The worker is mostly backend-agnostic. The differences:

| Backend | What it provides |
|---|---|
| `slurm` | inside an sbatch allocation; worker uses `SLURM_NTASKS`, `SLURM_JOB_ID` etc. |
| `ccm` *(Phase 2 — not implemented)* | CCM-managed Vast.ai instance; worker has CCM job ID, runs until destroyed |
| `local` | direct execution; useful for testing + small interactive workflows |

A backend is responsible for **submitting the worker itself** (the outer allocation). The pool + task code doesn't care.

```python
# Backend interface
class Backend:
    def submit_worker(self, *, cores, gpus, walltime, pool_dir) -> WorkerHandle: ...
    def status(self, handle) -> WorkerStatus: ...
    def cancel(self, handle) -> None: ...
```

## What's NOT in scope (deliberately)

These are intentionally excluded from MVP. Adding any requires a real use case to back it.

- Authentication / multi-tenancy / user isolation
- Web UI
- Real-time pub/sub (use polling + journal)
- Cluster federation across institutions
- Database backing store
- Workflow language / DSL (tasks are just YAML; DAG is just `depends_on`)
- Auto-scaling worker count
- Cost optimization (cheapest backend first)
- Conda env management (let user activate env in command)

## Open questions to resolve before code

- **Does CCM fold in?** Still open. Cross-analysis lives at `docs/CCM_INTEGRATION_ANALYSIS.md`; decision deferred to Phase 2.
- **Pool dir lifecycle**: `subjob archive` ships (Cycle 1 close-out 2026-05-29); journal rotation remains a manual operator step. Multi-week growth policy still TBD.
- **Workers with heterogeneous resources** (e.g., one worker on c128, one on c64): how does that affect priority/claiming?
- ~~**Heartbeats**: should workers write a heartbeat file every ~60s so the pool can detect dead claims?~~ **Yes — shipped 2026-05-30** (Cycle 2 Thrust 10). Claim-stamp + per-pool `.heartbeats/<worker_id>`; live workers self-heal a dead cohort-mate via auto-sweep. See `DEPLOYMENT.md §2`.
