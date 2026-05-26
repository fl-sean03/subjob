# CCM × subjob — cross-analysis

> Question: Can we retire CCM and fold its capabilities into subjob, or do they solve different problems and need to coexist?
>
> **Answer: They solve different problems. Don't retire CCM. Integrate it as a backend in subjob.**

Captured 2026-05-25.

---

## What CCM actually is (after a real audit)

CCM is **far more mature** than the GitHub README implies. From `~/Workspace/main/46-CCM/AGENTS.md`:

- ~16,000 LOC source + 6,500 LOC tests
- 390 unit tests passing
- Real production use: 10-day hydrogenation campaign, 601 job attempts, 18 snapshots
- Survived 20 separate hardening fixes (#7-#28) during that campaign
- Built on SQLModel + FastAPI + Typer

**CCM's problem domain**: make one cloud job survive a hostile cloud environment (Vast.ai spot instances that can be preempted any second). Features built around that:

- Spot preemption detection + auto-recovery
- Checkpoint detection (8 app types: NAMD, GROMACS, LAMMPS, QE, VASP, PyTorch Lightning, HF Trainer, generic)
- Restart adapter chain — auto-detects app, generates restart commands
- Multi-signal health checks (SSH + process + workspace + disk space)
- Per-job budget enforcement (max cost, max hours, max hourly rate)
- Continuous rsync sync (so checkpoints survive instance death)
- Reliability filters (residential-host blacklisting after burning 34% of jobs to bad hosts)
- SIGTERM-aware wrapper (catches preemption signal, writes checkpoint marker)
- Daemon reconciliation (rehydrates job state after daemon downtime)
- Web dashboard, REST API, agent SDK, CLI (45+ commands)
- 6 built-in templates, batch matrix expansion, multi-stage pipelines

**That is not a small project.** It's a production cloud management platform.

---

## What subjob solves (recap)

- Many tasks share one allocation
- Pilot/worker pattern: workers pull from a shared task pool
- HPC-native (SLURM) but backend-pluggable
- Avoids the 100× queue-wait penalty of submitting 100 individual sbatch jobs

**subjob's problem domain**: high-task-count fan-out where queue overhead dominates.

---

## The crucial observation

CCM treats **one cloud instance as one task**. Provision → run → destroy. With all the bells to make that one task survive whatever the cloud throws at it.

subjob treats **one allocation as N tasks**. The allocation is a worker that cycles through tasks until walltime ends.

These are different models, not competing models. They're complementary:

| Use case | Use what |
|---|---|
| One 24 hr NAMD production run on a $0.04/hr 4090 with spot resilience | **CCM** — that's exactly what it's designed for |
| 60 short analysis tasks (5 min each) across one HPC allocation | **subjob** — pilot pattern is the right tool |
| 60 short analysis tasks but on cloud because HPC queue is jammed | **subjob with a CCM-provisioned worker** — integration point |
| 20 medium NAMD ensemble members (15 ns each) on amilan | **subjob** with SLURM backend (current cool+prod problem) |
| 20 medium NAMD ensemble members but each needs spot resilience on Vast.ai 4090s | **subjob with CCM as backend** — CCM provisions the resilient cloud workers; subjob orchestrates the per-member task dispatch |

---

## Integration architecture

subjob already has a backend abstraction (see `ARCHITECTURE.md`). The backend's job is to **submit the worker**. The worker itself doesn't care where it's running — it just pulls from the pool and runs tasks.

### subjob backends

| Backend | What it provisions | Worker survives via |
|---|---|---|
| `slurm` (Phase 0) | sbatch allocation on Alpine | SLURM walltime |
| `ccm` (Phase 2) | Vast.ai instance with CCM resilience | CCM's preemption recovery (worker process restarts on the recovered instance, resumes pulling from pool) |
| `local` (testing) | Direct process | None — testing only |

### The CCM backend wraps CCM's existing job submission

```python
# subjob/backends/ccm.py (sketch)
class CCMBackend:
    def submit_worker(self, *, cores, gpus, walltime, pool_dir):
        # Build a CCM job YAML that runs the subjob worker
        ccm_job = {
            "command": f"python -m subjob.worker --pool {pool_dir}",
            "resources": {"gpu_type": "RTX_4090", "gpu_count": gpus, ...},
            "budget": {"max_cost_usd": 50.0, "max_hours": walltime/3600},
            "sync": {"enabled": True, "source": pool_dir, "destination": pool_dir},
            "checkpoint": {...},  # CCM handles checkpoint detection on worker process
        }
        result = ccm_client.submit(ccm_job)
        return WorkerHandle(backend="ccm", job_id=result.job_id)
```

CCM provides:
- Spot resilience (preemption recovery)
- Instance reliability filtering
- Cost enforcement
- Continuous sync (so the pool dir stays consistent across instance replacement)
- Health monitoring

subjob provides:
- Task pool semantics (the worker pulls from where CCM puts it)
- DAG between tasks
- Failure intelligence
- Multi-worker coordination (Sean could have one CCM worker AND one SLURM worker pulling from the same pool simultaneously)

This is a clean separation of concerns. Neither project absorbs the other; they compose.

---

## Should anything be retired?

**No. Both stay.**

- **CCM**: stays as its own project at `46-CCM/`. Still the canonical way to submit one-off long cloud jobs (the most common Vast.ai use case for the lab). The 16k LOC of preemption handling, checkpoint adapters, and resilience hardening is far too valuable to throw away.

- **subjob**: new project at `47-subjob/`. Solves the pilot-pattern problem that CCM is fundamentally not designed for.

- **fl-sean03/allocation-scheduler**: the original seed. Will be referenced in `subjob/README.md` as inspiration. No code reuse since the design has diverged significantly.

---

## What might LOOK like overlap but isn't

| Apparent overlap | Reality |
|---|---|
| Both have CLI | Different command surfaces. CCM: `ccm jobs submit`, `ccm benchmark`. subjob: `subjob submit-task`, `subjob pool-status`. |
| Both have agent SDKs | CCM SDK manipulates jobs (provision, status, exec). subjob SDK manipulates tasks (add to pool, follow, diagnose). |
| Both track jobs | CCM tracks "cloud instances doing one app run". subjob tracks "tasks within a pool, irrespective of where they ran". |
| Both have batch features | CCM batch = matrix expansion of one-instance jobs. subjob batch = many tasks sharing fewer allocations. |
| Both handle failures | CCM: preemption recovery + restart adapter. subjob: pool-level retry + prior matching. |

When in doubt: CCM = "make this one thing work despite cloud chaos". subjob = "manage N things across whatever workers I gave you".

---

## Migration / coexistence plan

Phase 2 of subjob (per `DEVELOPMENT.md`) builds the CCM backend. Before then, both stay independent. After then:

- CCM continues to be the right tool when you want to submit one Vast.ai job
- subjob with CCM backend is the right tool when you want a pool of tasks AND you want them on cloud workers

Lab agents can use both, picking the right one per workload. The skills (`.claude/skills/`) for each clarify when to reach for which.

---

## What about the fl-sean03/allocation-scheduler repo?

That's an upstream seed. subjob diverges enough that there's no benefit to staying compatible. We'll thank them in `README.md` and move on.

The fl-sean03 repo is single-file Python with a different architecture (in-allocation SQLite vs filesystem-broker pool). Trying to merge would compromise both.

---

## Bottom line

> Three projects:
> - **CCM** — one job, cloud, resilience-first. Mature. Keep.
> - **subjob** — many tasks, pluggable backends, agent-first. New. Build.
> - **fl-sean03/allocation-scheduler** — design inspiration. Acknowledge. Don't merge.
>
> The integration point: subjob's `ccm` backend. Phase 2 of development plan.
