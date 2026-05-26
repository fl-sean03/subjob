# Agent guide — subjob

For Claude (or any agent) using subjob from a session.

## When to reach for it

| You want to... | Use subjob? |
|---|---|
| Run 1 short task → produce 1 result | No, just sbatch |
| Run 1 long task (12+ hr) → 1 result | No, just sbatch |
| Run 5-500 short tasks (each < 1 hr) | **Yes** — queue overhead dominates without it |
| Run 20-100 medium tasks (1-6 hr each) with diverse params | **Yes** — DAG + retry + diagnostic centralized |
| Run a Bayesian-opt loop where next task depends on results | **Yes** — dynamic add-task, depends_on |
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
pool.ensure_workers(backend="slurm", count=2, profile="amilan-c64-long")

# Watch
for event in pool.follow(timeout_s=24*3600):
    # event is a dict: {type, task_id, timestamp, payload}
    match event["type"]:
        case "task_done":
            print(f"  ✓ {event['task_id']}")
        case "task_failed":
            diag = pool.diagnose(event["task_id"])
            # diag includes prior-match if any, stdout/stderr tail, classifier verdict
            print(f"  ✗ {event['task_id']}: {diag['classification']}")
```

## CLI (for Bash invocation from agents)

```bash
# Submit
subjob submit --pool /scratch/.../pool --task-file tasks.yaml
# → prints task ids, exit 0

# Status (JSON, machine-readable)
subjob status --pool /scratch/.../pool --format json
# → {"pending": 17, "claimed": 0, "running": 3, "done": 0, "failed": 0}

# Follow (stream events as JSONL)
subjob follow --pool /scratch/.../pool --since-event-id 0
# → emits one JSON per line, blocking until terminated

# Diagnose a failure
subjob diagnose --pool /scratch/.../pool --task-id snap_005
# → prints classification + suggested fix + log tails

# Ensure workers exist
subjob ensure-workers --pool /scratch/.../pool --backend slurm --count 2 \
    --profile amilan-c64-long
```

## What an agent should do when a task fails

1. Get the diagnosis: `subjob diagnose --pool $POOL --task-id $TID`
2. If diagnosis matches a prior with auto-mitigation → already retried, just wait
3. If diagnosis is a known prior without auto-mitigation → apply the suggested fix (or escalate to user if destructive)
4. If diagnosis is "unknown failure" → catalog it in `simulations/.priors.yaml`, escalate to user

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
pool.ensure_workers(backend="slurm", count=4, profile="amilan-c32-normal")
pool.follow_until_done()
```

### Pattern: parameter sweep with DAG

```python
pool = Pool(...)
build_tasks = [Task(id=f"build-{p}", command=f"build_system.py --param {p}",
                    resources={"cores": 2}) for p in params]
run_tasks = [Task(id=f"run-{p}", command=f"namd3 ... --param {p}",
                  resources={"cores": 32}, depends_on=[f"build-{p}"]) for p in params]
analyze_tasks = [Task(id=f"analyze-{p}", command=f"...",
                      depends_on=[f"run-{p}"]) for p in params]
pool.submit_batch(build_tasks + run_tasks + analyze_tasks)
pool.ensure_workers(count=4)
pool.follow_until_done()
```

### Pattern: dynamic task generation (Bayesian opt)

```python
pool = Pool(...)
initial = [Task(id=f"init-{i}", command=...) for i in range(5)]
pool.submit_batch(initial)
pool.follow_until_state("done", task_ids=[t.id for t in initial])

# Read results, decide next sample
results = [pool.read_artifact(tid, "result.json") for tid in initial_task_ids]
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
