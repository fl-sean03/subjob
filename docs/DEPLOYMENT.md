# Deployment contracts & operational notes

What you need to know before running subjob for real campaigns (not just
the validation suite). Read alongside `AGENT_GUIDE.md` (how to use it) and
`validation/RESULTS.md` (what's been proven).

---

## 1. The task contract (read this before submitting real work)

### 1.1 Commands must be restart-safe (idempotent or resumable)

A task can be **run more than once**. If a worker is preempted or hits its
allocation walltime mid-task, subjob **releases the claim** and another
worker **re-runs the same command from the start**. subjob does not
checkpoint your task — it only re-dispatches it.

Therefore your command must be one of:
- **Idempotent** — re-running from scratch produces the right result
  (e.g., `analyze.py --in X --out Y` that overwrites Y).
- **Resumable** — the command itself detects existing progress and
  continues (e.g., NAMD reading its latest `.restart` files, or a guard
  like `[ -f DONE ] && exit 0` at the top).

A command that is **neither** (appends to a file, increments a counter,
charges money) can corrupt or double-count on re-execution. Don't submit
those without a guard.

**Cap on retries:** a task released `retry.max_attempts` times (default
**3**) without completing is moved to `failed/` rather than bouncing
forever. Set `retry: {max_attempts: N}` in the task YAML to change it.
This protects against a task that needs more walltime than any worker has.

### 1.2 Working directory & environment

The worker runs your command with `shell=True` as the worker's user. By
default the process inherits the worker's cwd (wherever the sbatch wrapper
launched). Two ways to control where it runs:
- Set `workdir:` in the task YAML — the runner `cd`s there first
  (env-vars and `~` are expanded).
- Or `cd` explicitly inside the command.

The task inherits the worker's full environment (including SLURM vars like
`CUDA_VISIBLE_DEVICES`) plus anything in the task's `env:` map. subjob does
**not** activate conda/modules for you — do that inside the command
(`module load ...; mycmd`).

### 1.3 Walltime

`resources.walltime_seconds` is enforced: the runner kills the task (SIGTERM
→ 5 s grace → SIGKILL) if it exceeds that. A worker also refuses to *start*
a task whose declared walltime won't fit in its remaining allocation budget
(minus a safety margin). **Declare walltime tightly** — generous values
both waste the kill-timer and make the fit check refuse the task near
allocation end.

---

## 2. Preemption & failure recovery

| Event | What subjob does | What you must do |
|---|---|---|
| Worker hits allocation walltime | Releases in-flight claims → `pending/`; they're re-run by the next worker | Ensure commands are restart-safe (§1.1) |
| Worker gets SIGTERM (SLURM preempt / scancel) | Same — clean release, kills the in-flight subprocess so it doesn't orphan or double-run | Nothing |
| Task exceeds its own `walltime_seconds` | Killed, moved to `failed/` with `walltime_killed=True` | Raise the task's walltime or split the work |
| Task exits non-zero / segfaults | Moved to `failed/` with the exit code recorded | Diagnose from `failed/<id>.yaml` attempts + `logs/<id>.err` |
| **Worker node dies** (crash, network loss) | **Detected automatically** by other live workers via missing/stale heartbeat (`<pool>/.heartbeats/<worker_id>`). Stale claims are released back to `pending/` (event: `task_released` with `reaped_stale: True`) so a live worker retries them. | Nothing, when any other worker is running. If the whole cohort died, run `subjob reap-stale [--auto]` (below). |

### Dead-worker recovery: heartbeats + `subjob reap-stale`

Each worker touches `<pool>/.heartbeats/<worker_id>` every ~30s (configurable
via `--heartbeat-interval`; 0 disables). Each worker also periodically sweeps
`claimed/` (every `--auto-reap-interval` seconds, default 120s) and releases
any claim whose stamped `claimed_by` owner has no heartbeat or a heartbeat
older than ~4× the heartbeat interval. A worker never reaps its OWN claims.
Result: a live cohort self-heals from a single dead node without operator
intervention.

`subjob reap-stale` remains as an operator escape hatch — useful when the
entire cohort is gone, or to force an immediate sweep:

```bash
# Heartbeat-aware sweep (preferred when workers stamp claims). Default
# --older-than for --auto is 120s (= 4× the worker heartbeat interval).
subjob reap-stale --pool /scratch/.../pool --auto --older-than 120

# Legacy mtime-only sweep (Phase 0 behavior; still works on un-stamped claims):
# moves claims older than 2h back to pending/. Use a threshold safely LARGER
# than your longest task's walltime.
subjob reap-stale --pool /scratch/.../pool --older-than 7200 --to pending

# Preview without moving:
subjob reap-stale --pool /scratch/.../pool --auto --older-than 120 --dry-run

# Give up on them instead (move to failed/):
subjob reap-stale --pool /scratch/.../pool --auto --older-than 120 --to failed
```

- `--auto` uses **heartbeats** (`<pool>/.heartbeats/<worker_id>`) as the
  primary liveness signal. Per-task `reason` strings reported in the JSON:
  `owner_missing` (no heartbeat file for the stamped owner), `owner_stale_Ns`
  (heartbeat older than the threshold), `legacy_mtime_Ns` (no `claimed_by`
  stamp — pre-thrust claim, falls back to file mtime).
- Without `--auto`, `reap-stale` **uses file mtime as its only liveness
  signal**. A task still legitimately running but older than `--older-than`
  can be reaped and re-run *while the original is still executing* — set
  `--older-than` comfortably above your longest task's walltime, and rely on
  restart-safe commands (§1.1) to make a rare double-run harmless. (With
  `--auto` this concern is moot for stamped claims: heartbeats positively
  identify the owner.)

---

## 3. Concurrency & correctness guarantees

- **At-most-once claim:** validated to 1000 tasks across 8 nodes, zero
  double-claims (atomic `os.rename`). Two workers never run the same task
  simultaneously.
- **Concurrent submitters:** `submit()` uses exclusive create (`os.link`),
  so two agents racing on the same task id → one wins, the other gets a
  clear duplicate-id error. No silent clobber.
- **Cross-dir re-submit is only soft-guarded (low-probability TOCTOU):**
  `submit()`'s exclusive `os.link` only protects against two concurrent
  submitters clobbering an id in `pending/`. Re-submitting an id that is
  already in `done/`/`failed/`/`claimed/` is caught by a soft existence
  check (`_task_exists_anywhere`) with a check-then-act gap — a concurrent
  finalize landing between the check and the link could in principle slip a
  duplicate through. Task ids should be **unique by construction**; don't
  re-use a terminal id within a live pool.
- **Journal integrity:** appends are serialized with `flock` + single
  `os.write`; validated at 20 k events from 8 concurrent threads, zero
  torn lines. Readers also skip any unparseable line defensively.
- **File state is authoritative; the journal is advisory.** State counts
  come from counting files in `pending/claimed/done/failed`, not from the
  journal. If a worker crashes between moving a file to `done/` and writing
  the `task_done` event, the task is still correctly counted as done — the
  missing event is benign. Don't build logic that assumes every state
  transition has a journal event.

---

## 4. Scale & operational limits (know these before a big campaign)

| Concern | Current behavior | Guidance |
|---|---|---|
| **Pending pool size** | Worker poll cost is O(files in pending/) per cycle for the directory stat; parsed YAMLs are cached (F-001 fix). ~30 ms/poll at 5000 pending. | Comfortable to a few thousand pending. For 10k+, split into multiple pools. |
| **Submission rate** | GPFS-metadata-bound: ~30 tasks/s on `/scratch` (vs ~2300/s on local tmpfs). Each submit ≈ 6 metadata ops. | 60-task dogfood = ~2 s. 5000 tasks = ~3 min of submit. Submit from a script, not interactively, for big batches. |
| **Throughput** | ~4 tasks/s per 8-core worker on GPFS (metadata-bound, trivial tasks). Scales ~linearly with worker count (13/s at 8 workers). | For many short tasks, add workers rather than cores. Real multi-minute tasks are compute-bound, not metadata-bound — this only matters for very short tasks. |
| **`logs/`, `done/`, `journal.jsonl` growth** | Unbounded — never rotated automatically. `subjob archive` gzips old `done/`/`failed/` YAMLs into `<pool>/archive/`; the journal stays a manual rotation step. | For multi-week campaigns, periodically run `subjob archive --pool <P> --older-than-days N [--include-failed]` (and separately truncate the journal), or cycle to a fresh pool. `read_journal()` loads the whole journal into memory — keep journals to ~100k events. |
| **Disk / quota** | A task that fills the disk fails like any non-zero exit; a worker that can't write the journal/logs will error. No pre-flight quota check. | Watch `/scratch` quota during long campaigns. `/scratch` has retention — checkpoint long-lived pools to `/projects`. |
| **Clock skew** | `event_id = time.time_ns()` per node; cross-node journal ordering assumes NTP-synced clocks (Alpine is). | Fine on a single cluster. Don't rely on journal ordering across clusters with unsynced clocks. |

- **Unclaimable tasks block `follow_until_done()` forever.** A task whose
  `resources.cores` (or `gpus`) exceeds *every* worker's capacity is never
  claimed; workers idle-exit and leave it pending. `follow_until_done()` /
  `follow_until_state()` poll until the pool drains, so without a bound they
  block indefinitely. **Always pass a `timeout_s`** to those calls, and size
  tasks to fit at least one worker.

---

## 5. Validation status (real hardware)

**Validated end-to-end on Alpine:**
- **Real MD binary + GPU passthrough** — NAMD3 ran a real LJ argon sim
  through subjob on a real A100 (`atesting_a100`); the task received
  `CUDA_VISIBLE_DEVICES` and saw the GPU (Tier X, `validation/RESULTS.md`).
- **CPU partition** (`amilan`) — extensively, up to 1000 tasks × 8 nodes.
- **Concurrency, preemption, recovery** — see RESULTS tiers + Cycle 1.

**Environment-blocked on Alpine (subjob dispatches them fine — the blocker
is the cluster's software env, not subjob):**
- **LAMMPS / GROMACS / Quantum ESPRESSO** — the CURC builds need a
  module/shared-lib chain that didn't fully resolve; LAMMPS specifically
  links a `libkim-api.so.2` that isn't installed. To run these, load the
  correct modules inside the task `command` (subjob does not manage
  modules) and confirm the binary runs standalone first.
- **MPI inside a worker** (`mpirun -n N` within one allocation) —
  `mpirun` is module-gated on Alpine (not on the default PATH); load the
  MPI module in the task command. Architecturally a worker is one node, so
  multi-node MPI per task is out of scope; single-node MPI should work once
  the module env is set.

**Phase-1 infra pulled forward (Cycle 2, 2026-05-30):** task DAG /
`depends_on` enforcement, artifact validation, heartbeats + automatic
stale-claim recovery, and the priors framework / `pool.diagnose` are all
now shipped. See `validation/RESULTS.md` Cycle 2 record for the commit
hashes and verdict. The remaining roadmap (still deliberately deferred):
CCM / Vast.ai cloud backend, multi-GPU type accounting, cost-aware
backend selection, and `pool.read_artifact` (a one-call read+verify
helper — note that validation itself is shipped). See
`validation/RESULTS.md` "Phase 1 / Phase 2" table for the up-to-date
list.

---

## 6. Quick pre-campaign checklist

- [ ] Task commands are idempotent or resumable (§1.1)
- [ ] Walltimes declared tightly and realistically (§1.3)
- [ ] `module load` / env activation is inside the command, not assumed
- [ ] Pool is on a single shared filesystem (`/scratch`), not split
- [ ] Pending count will stay under a few thousand (else split pools)
- [ ] You know the `reap-stale` recovery command for dead nodes (§2)
- [ ] For multi-week runs: a plan to rotate/archive the pool (§4)
- [ ] GPU/MPI tasks: smoke-tested one before the full batch (§5)
