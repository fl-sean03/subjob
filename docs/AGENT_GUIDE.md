# Agent guide — subjob

For Claude (or any agent) using subjob from a session.

## When to reach for it

| You want to... | Use subjob? |
|---|---|
| Run 1 short task → produce 1 result | No, just sbatch |
| Run 1 long task (12+ hr) → 1 result | No, just sbatch |
| Run 5-500 short tasks (each < 1 hr) | **Yes** — queue overhead dominates without it |
| Run 20-100 medium tasks (1-6 hr each) with diverse params | **Yes** — centralized retry + triage (inter-stage ordering: stage manually with `follow_until_state`; `depends_on` DAG is Phase 2) |
| Run a Bayesian-opt loop where next task depends on results | **Yes** — submit a batch, `follow_until_state`, read results, submit the next batch |
| Run analysis pipeline across N snapshots | **Yes** — canonical pilot case |
| Submit one big NAMD production run | No, just sbatch |

## Mental model

A `subjob` **pool** is a directory on `/scratch` containing task YAMLs.
**Workers** (each one is itself a SLURM allocation) pull tasks from the pool.
**You** (agent or human) write task YAMLs to the pool and watch the **journal** for events.

## API (Python, agent-facing)

```python
from subjob import Pool, Task

pool = Pool("/scratch/alpine/sefl7948/pools/<project>-<purpose>")

# Add tasks
task_ids = pool.submit_batch([
    Task(
        id=f"snap_{i:03d}",
        command=f"cd /scratch/.../snapshot_{i:03d} && /hydrog-run-pipeline G1G2",
        resources={"cores": 4, "memory_gb": 8},
        artifacts={"expect": ["outputs/orientation_breakdown.json"],
                   "success_marker": {"file": "outputs/run.log", "contains": "ANALYSIS COMPLETE"}},
    )
    for i in range(1, 21)
])

# Make sure workers exist (sbatch them if not)
pool.ensure_workers(
    backend="slurm", count=2, cores=64,
    partition="amilan", qos="long", idle_timeout_seconds=600,
)

# Watch
for event in pool.follow(timeout_s=24*3600):
    # event is a dict: {type, task_id, timestamp, payload}
    if event["type"] == "task_done":
        print(f"  ✓ {event['task_id']}")
    elif event["type"] == "task_failed":
        t = pool.read_task("failed", event["task_id"])
        last = t.attempts[-1] if t.attempts else {}
        print(f"  ✗ {event['task_id']}: exit={last.get('exit_code')} {last.get('error')}")
```

## CLI (for Bash invocation from agents)

```bash
# Submit
subjob submit --pool /scratch/.../pool --task-file tasks.yaml
# → prints task ids, exit 0

# Status (JSON, machine-readable)
subjob status --pool /scratch/.../pool --format json
# → {"pending": 17, "claimed": 3, "done": 0, "failed": 0}
#   (claimed = in-flight; there is no separate "running" key)

# Follow (stream events as JSONL)
subjob follow --pool /scratch/.../pool --since-event-id 0
# → emits one JSON per line, blocking until terminated

# Triage failures (read-only): exit codes, walltime-kill, error, stderr tail
subjob failures --pool /scratch/.../pool
# → {"failures": [{"task_id": "snap_005", "exit_code": 1, ...}], "count": 1}

# Drill into one failed task (adds command + a longer stderr tail)
subjob failures --pool /scratch/.../pool --task-id snap_005
```

## What an agent should do when a task fails

1. Triage with the CLI: `subjob failures --pool $POOL` (all failures) or
   `subjob failures --pool $POOL --task-id $TID` (one task, with command + longer stderr tail).
2. For programmatic access from Python, list and read failed tasks directly:
   `pool.list_state("failed")` returns the failed task paths, and
   `pool.read_task("failed", id)` loads a `Task` whose `.attempts[-1]` holds the
   last attempt's `exit_code` / `walltime_killed` / `error`.
3. Decide a fix from the exit code + stderr tail, then re-submit a corrected task
   (or escalate to the user if the fix is destructive or the cause is unknown).

## Common patterns

### Pattern: per-snapshot analysis fan-out

```python
pool = Pool(f"/scratch/.../pools/hydrog-analysis-{date}")
pool.submit_batch([
    Task(id=f"{campaign}-{snap}",
         command=f"/hydrog-run-pipeline ... --snapshot {snap}",
         resources={"cores": 4})
    for campaign in ["Pt100", "Pt111", "Pt110"]
    for snap in range(1, 21)
])
pool.ensure_workers(backend="slurm", count=4, cores=32, partition="amilan", qos="normal")
pool.follow_until_done()
```

### Pattern: staged parameter sweep (build → run → analyze)

> **`depends_on` is NOT enforced yet** (DAG is Phase 2 — see "not yet
> implemented" below). A task carrying `depends_on` is parsed but the worker
> will dispatch it as soon as a core is free, ignoring the dependency — which
> runs dependents on absent inputs. **Until DAG lands, order stages yourself**
> by draining one stage before submitting the next, with `follow_until_state`:

```python
pool = Pool(...)
pool.ensure_workers(backend="slurm", count=4, cores=32, partition="amilan", qos="normal")

build = [Task(id=f"build-{p}", command=f"build_system.py --param {p}",
              resources={"cores": 2}) for p in params]
pool.submit_batch(build)
pool.follow_until_state("done", task_ids=[t.id for t in build])   # gate stage 1

run = [Task(id=f"run-{p}", command=f"namd3 ... --param {p}",
            resources={"cores": 32}) for p in params]
pool.submit_batch(run)
pool.follow_until_state("done", task_ids=[t.id for t in run])     # gate stage 2

analyze = [Task(id=f"analyze-{p}", command="...") for p in params]
pool.submit_batch(analyze)
pool.follow_until_done()
```

### Pattern: dynamic task generation (Bayesian opt)

```python
pool = Pool(...)
initial = [Task(id=f"init-{i}", command=...) for i in range(5)]
pool.submit_batch(initial)
initial_task_ids = [t.id for t in initial]
pool.follow_until_state("done", task_ids=initial_task_ids)

# Read results, decide next sample. Phase 0 has no artifact helper, so read
# the result file the task wrote yourself (you control the command + path):
import json
results = [json.loads((scratch / tid / "result.json").read_text()) for tid in initial_task_ids]
next_samples = bayes_opt.suggest(results)

pool.submit_batch([Task(id=f"sample-{j}", command=...) for j in next_samples])
# (loop until convergence)
```

## Things to be careful of

- **Don't write to the same artifact path from multiple tasks** — pool doesn't enforce this
- **Pool dir on `/scratch`** — survives node failure but `/scratch` has retention policy. For long-running pools, periodic checkpoint to `/projects`.
- **Task command runs as the worker's user** — same UID as whoever submitted the worker sbatch
- **No automatic restart on FATAL** unless `retry.max_attempts > 0` and prior matches
- **Walltime is per-task** — if a task hits its declared walltime, worker kills it. Don't set generous values "just in case"; tighter is better.
- **Workers exit at allocation walltime** — unfinished claims get released and re-queued. Plan for this if your worker allocation is 7 days but tasks are 12 hr.

## Artifact validation

subjob validates a task's declared `artifacts` after exit 0. A command that
exits 0 without writing its declared outputs is recorded as a **real failure**,
not a silent wrong result.

Two kinds of artifacts are checked:

- `artifacts.expect`: list of paths that must exist after the command finishes.
- `artifacts.success_marker`: `{file, contains}` — the file must exist AND
  contain the given substring. (`contains` is optional; omit it to require only
  that the file exists.)

Path resolution:

- `$VAR` and `${VAR}` are expanded against the task's `env` dict (NOT the
  worker process's environ).
- `~` is expanded against `env["HOME"]` if set, else the process's `HOME`.
- Relative paths resolve against the task's `workdir` (or the worker's cwd if
  the task declares none).

When validation fails, the attempt recorded under `failed/` carries
`artifact_validation_failed: True` plus an `artifact_detail` dict containing
the resolved paths checked (`expect_checked`), any `missing_expect`, and the
success-marker outcome (`success_marker_path`, `success_marker_found`,
`success_marker_contains` / `success_marker_missing`). `subjob failures
--task-id <id>` surfaces these fields directly. When validation succeeds, the
same detail is attached under `artifacts` on the done attempt.

## Not yet implemented (Phase 1 / Phase 2)

These are **parsed but not acted on**, or not present at all. Don't rely on
them yet:

- **`depends_on` / task DAG — Phase 2.** A `Task` accepts `depends_on=[...]`
  and it round-trips in the YAML, but the worker does **not** enforce it: a
  dependent task is dispatched as soon as a core frees, regardless of whether
  its dependencies are done. Order stages yourself with `follow_until_state`
  (see "staged parameter sweep" above) until DAG lands.
- `pool.diagnose(task_id)` — *Phase 1* — classifier verdict + prior-match +
  suggested fix. Until then, use `subjob failures` / `pool.read_task("failed",
  id)` for triage (see "What an agent should do when a task fails" above).
- `pool.read_artifact(task_id, name)` — *Phase 1* — validated read of a task's
  declared artifact. Until then, read the result file your task wrote directly
  (you control the command and output path).
