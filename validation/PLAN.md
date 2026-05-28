# subjob Validation & Benchmarking Plan

> **Status:** Drafted 2026-05-27 after Phase 0 ships. Pre-condition for any project-specific (MXene / hydrogenation) dogfood.
>
> **Scope:** Lab-agnostic, ground-up validation of `subjob` across the full HPC software stack we run on Alpine. Iterative scaling, gates between tiers, remediation playbook for every expected failure class.

---

## 0. Goals & non-goals

### Goals

1. **Coverage** — exercise subjob with every HPC compute package we routinely use (LAMMPS, GROMACS, Quantum ESPRESSO, NAMD, PyTorch, generic Python), plus all the synthetic edge cases (failure modes, concurrency, recovery).
2. **Reproducibility** — every test is a Python function that any agent can re-run. No hand-rolled invocations.
3. **Iterative scaling** — start at trivial scale, escalate one variable at a time. Each escalation must pass its gate before the next is attempted.
4. **Diagnosability** — when a gate fails, the report tells you WHAT failed (which task, which event, which assertion) so you can remediate without re-running.
5. **Lab-agnostic** — no project-specific data. Use only public/canonical inputs (LJ liquid for LAMMPS, Si bulk SCF for QE, etc.) so the suite stays portable.

### Non-goals

- Project-specific workloads (MXene, hydrogenation) — those run AFTER validation completes
- Phase 2+ features (DAG, GPU resource accounting beyond a counter, CCM backend)
- Comparison benchmarks vs other pilot schedulers (FireWorks, Parsl, etc.)
- Cluster-federation / cross-institution scenarios

---

## 1. What's already been validated (and what's missing)

| # | Stage | What it proved | Where it lives |
|---|---|---|---|
| 0 | Install + 65 pytest on Alpine | Code installs under Python 3.10.2, all unit tests pass | (one-shot, not in harness) |
| 1 | Local worker on `/scratch` | Atomic rename works on GPFS | (one-shot) |
| 2 | One sbatch worker | SLURM backend submits + worker drains real-cluster pool | (one-shot) |
| 3 | Walltime kill + nonzero exit | Failure classification + attempt records | (one-shot) |
| 4 | Two concurrent workers, 20 tasks | No double-claims across processes | (one-shot) |
| 5 | 4-core worker, 12 tasks | Internal `ThreadPoolExecutor` saturates capacity | (one-shot) |

**Gaps this plan closes:**
- Every real binary (LAMMPS, GROMACS, QE, NAMD, PyTorch) is untested
- All adversarial failure modes are untested (segfault, OOM, SIGTERM-ignore, infinite stdout)
- Scale is ≤20 tasks; production load is hundreds
- Cross-node race never exercised (Stage 4 happened to land on one node)
- SLURM SIGTERM at allocation walltime never exercised
- Mid-task scancel / dead-worker recovery untested
- No performance baseline → can't detect regressions

---

## 2. Test architecture — the `validation/` harness

Everything below lives under `validation/` in the subjob repo. Stdlib-only, like the rest.

```
validation/
├── PLAN.md                       ← this file
├── __init__.py
├── workloads.py                  ← Category A: synthetic task generators
├── gates.py                      ← pass/fail check primitives (composable)
├── runner.py                     ← submit a tier, poll, evaluate gates, write report
├── report.py                     ← markdown report writer
├── inputs/                       ← minimal lab-agnostic input files
│   ├── lammps/in.lj              ← LJ liquid benchmark
│   ├── gromacs/                  ← water box NVT
│   ├── qe/si.scf.in              ← Si bulk SCF (standard tutorial)
│   ├── namd/                     ← apoa1 vendor benchmark (subset)
│   └── pytorch/tiny_train.py     ← 1-batch synthetic CNN
├── binaries/                     ← task YAML templates per binary
│   ├── lammps_lj.yaml.tmpl
│   ├── gromacs_water.yaml.tmpl
│   ├── qe_si.yaml.tmpl
│   ├── namd_apoa1.yaml.tmpl
│   ├── pytorch_tiny.yaml.tmpl
│   └── numpy_svd.yaml.tmpl
└── tiers/
    ├── tier_0_harness.py
    ├── tier_1_synthetic.py
    ├── tier_2_binaries.py
    ├── tier_3_failure.py
    ├── tier_4_concurrency.py
    ├── tier_5_recovery.py
    ├── tier_6_perf.py
    └── tier_7_platforms.py
```

### `workloads.py` — synthetic task generators

Each generator is a `def make_<name>(id, **params) -> Task`. Categories:

| Generator | What it tests | Default params |
|---|---|---|
| `cpu_spin(seconds)` | Worker handles long-running CPU-bound work | 5 s |
| `mem_alloc(gb, hold_s)` | Memory accounting, OOM behavior | 1 GB / 2 s |
| `disk_io(mb, dir)` | Per-task scratch writes, log capture | 100 MB to /tmp |
| `stdout_firehose(lines)` | Log tailing + 4 KB tail truncation | 10 000 lines |
| `signal_grace(grace_s)` | Catches SIGTERM, exits clean | 1 s |
| `signal_ignore` | Ignores SIGTERM, forces SIGKILL fallback | — |
| `exit_code(rc)` | Exact exit code propagation | 0–255 |
| `fork_children(n)` | Subprocess cleanup on worker exit | 4 |
| `sleep_random(lo, hi)` | Variable-duration load for scheduling tests | 1–5 s |

All synthetic tasks: shell + standard tools only (bash, dd, python -c). No external deps.

### `gates.py` — pass/fail primitives

Composable predicates. Each returns `GateResult(passed: bool, name: str, detail: str)`.

```python
def status_equals(expected: dict) -> Gate                         # final status counters
def all_tasks_in(state: str, expected_ids: set[str]) -> Gate
def task_attempt(task_id: str, key: str, expected) -> Gate        # check attempts[-1][key]
def no_double_claims() -> Gate
def journal_event_present(event_type: str, task_id: str) -> Gate
def journal_event_absent(event_type: str) -> Gate
def task_duration_within(task_id: str, lo: float, hi: float) -> Gate
def task_stdout_contains(task_id: str, substring: str) -> Gate
def task_stderr_empty(task_id: str) -> Gate
def worker_capability_respected() -> Gate                          # no over-subscription
def concurrent_at_peak(min_concurrent: int) -> Gate                # peak parallel ≥ N
def throughput_at_least(tasks_per_s: float) -> Gate
```

Gates take a `Pool` and return a result. The runner aggregates them per tier.

### `runner.py` — tier executor

```python
def run_tier(
    tier: TierSpec,
    pool_root: Path,
    backend: str,                  # 'local' | 'slurm'
    *,
    workers: list[WorkerSpec],     # for slurm: cores, partition, qos, nodelist
    timeout_s: float,
    keep_pool: bool = False,       # leave pool on disk for forensics
) -> TierReport
```

The runner:
1. Creates a fresh pool under `pool_root`
2. Calls the tier's `submit(pool)` function
3. Submits workers via the requested backend
4. Polls the pool until status is settled (all tasks in done/failed) OR timeout
5. Evaluates every gate in the tier's spec
6. Writes a markdown report (per-gate pass/fail with detail) under `pool_root/REPORT.md`
7. Either keeps the pool or wipes it

### `report.py` — markdown report writer

Produces a deterministic markdown file:
```
# Tier <N> report — <timestamp>

Pool: <path>
Backend: <slurm|local>
Workers: [...]
Duration: <s>

## Gates

✓ PASS  status_equals  → {pending: 0, claimed: 0, done: 12, failed: 0}
✓ PASS  no_double_claims  → 12 unique claims
✗ FAIL  task_duration_within('cpu_spin_05', 4.5, 5.5)  → actual 7.2s
        Remediation: see PLAN.md § Remediation > "duration exceeds bound"

## Summary
11/12 gates passed. FAILED tier: tier_1_synthetic.
```

---

## 3. Workload taxonomy

Categories below cross-cut the tiers; one task can be both a Category A workload and a Category D concurrency test.

### Category A — synthetic compute primitives (lab-agnostic)
See `workloads.py` table above.

### Category B — real binaries (lab-agnostic inputs)

| Binary | Input | Expected duration | Resources | Gate marker in stdout |
|---|---|---|---|---|
| LAMMPS | LJ liquid, 4000 atoms, 1000 steps | ~5 s | 1 core | `Total wall time:` |
| GROMACS | TIP4P water 512 mols, 1 ps NVT | ~10 s | 1 core | `Finished mdrun` |
| Quantum ESPRESSO | Si bulk, 2-atom SCF, ecutwfc=20 | ~10 s | 1 core | `convergence has been achieved` |
| NAMD | apoa1 (apolipoprotein), 100 steps | ~15 s | 1 core | `End of program` |
| PyTorch | Synthetic CNN, 5 batches | ~30 s | 1 GPU + 4 cores | `Final loss:` |
| numpy SVD | 2000×2000 SVD | ~5 s | 1 core | `SVD complete:` |

**Inputs all public, downloaded from canonical sources OR bundled in `validation/inputs/`.** Total inputs <1 MB.

### Category C — failure injection

| Task | Mechanism | Expected outcome |
|---|---|---|
| `segfault` | `python -c "import ctypes; ctypes.string_at(0)"` | failed/, exit_code < 0 |
| `oom_kill` | `python -c "x=b'x'*(10**11)"` | failed/, exit_code < 0 OR walltime_killed |
| `sigterm_ignore` | trap SIGTERM in bash, sleep 60 with walltime 2 | failed/, walltime_killed=True, SIGKILL fired |
| `infinite_stdout` | `yes | head -c 100MB` | done/, stdout_tail is last 4KB only |
| `nonzero_matrix` | exit codes [1, 7, 127, 137, 255] | each in failed/, exit_code matches |
| `slow_start` | `sleep 3 && echo hi` with walltime 1 | failed/, walltime_killed=True before echo |

### Category D — concurrency stress

| Sub-tier | Tasks | Workers × cores | Goal |
|---|---|---|---|
| D.a | 50 | 2 × 1 | Calibrate harness; matches Phase 0 Stage 4 baseline |
| D.b | 200 | 4 × 4 | 4× scale, 4-way concurrency per worker |
| D.c | 500 | 4 × 4 | Same workers, longer pool — surfaces queue depth issues |
| D.d | 1000 | 8 × 8 | Production-like load (close to real per-snapshot fan-out) |
| D.x | 100 | 4 × 2 with `--exclusive` | Force cross-node race — superseded by D.d which naturally landed on 8 unique nodes |

**Note 2026-05-27:** Tier IV.d incidentally exercised the cross-node case
when SLURM distributed its 8 workers across 8 different compute nodes
(c3cpu-{a5-u1-2, a5-u7-4, a7-u3-4, a9-u13-4, a9-u3-3, c11-u13-2,
c11-u15-1, c9-u20}). All 1000 tasks were claimed exactly once. The
explicit `--exclusive` variant of IV.x is therefore redundant and queues
slowly — leave it as an optional manual check.

### Category E — recovery / chaos

| Scenario | Setup | Expected outcome |
|---|---|---|
| E.1 mid-task scancel | submit, wait until claimed, scancel worker | task released to pending, next worker picks up |
| E.2 corrupt YAML | manually `truncate -s 0 pending/X.yaml` after submit | worker moves to failed/ + logs error, continues |
| E.3 huge pool | submit 10 000 tasks, then run 1 worker | claim still finds work in <1s; no OOM in worker |
| E.4 walltime SIGTERM | 5-min sbatch wall, 6-min tasks | unfinished claims released; new worker picks them up |
| E.5 manual file ops | user `mv` a claimed file back to pending | worker handles the duplicate state gracefully |

### Category F — performance baselines

Recorded, not gated (we establish numbers; later runs check for regression).

| Metric | How measured |
|---|---|
| Tasks/sec (sustained) | 200 trivial echo tasks; time from worker_started to last task_done |
| Claim latency (p50/p99) | journal `task_submitted` → `task_claimed` per task |
| Journal write rate | wall time per emit at scale |
| Submit throughput | time to `submit_batch` 1000 tasks via Python API |
| Pool listing time at N=10 000 | time for `pool.pending_paths()` |

### Category G — platforms

| Partition | Canonical test | Why |
|---|---|---|
| `amilan` | LAMMPS LJ | CPU-only, default workload home |
| `al40` | PyTorch tiny | RTX 8000 GPU partition |
| `aa100` | PyTorch tiny + cuda check | A100 GPU partition |
| `blanca` | LAMMPS LJ | preemptible/research partition (may need wait) |

---

## 4. Tier plan with iterative scaling

Each tier has: **goal**, **pre-conditions** (gates from earlier tiers that must pass), **what we learn**, **gates**, **cost**, **remediation pointer**.

### Tier 0 — Harness self-test

| | |
|---|---|
| **Goal** | Catch harness bugs before they masquerade as subjob bugs |
| **Pre-conditions** | none |
| **What we learn** | Every gate predicate fires correctly on synthetic inputs |
| **Workload** | 5 trivial sleep tasks; 1 local worker; tasks designed to intentionally fail specific gates |
| **Gates** | Every gate predicate is exercised at least once with both pass + fail inputs |
| **Cost** | None (login-node, ~30 s) |
| **Pass criterion** | All harness unit tests pass; runner produces a valid REPORT.md |

### Tier I — Synthetic compute primitives

| | |
|---|---|
| **Goal** | Worker handles arbitrary commands, not just `echo` and `sleep` |
| **Pre-conditions** | Tier 0 passed |
| **What we learn** | If a real binary surprises us, it's not because Python's `subprocess.Popen(shell=True)` is broken — that's tested here. |
| **Workload** | Cat A: cpu_spin × 3 (1s, 5s, 10s) + mem_alloc × 2 (1 GB, 4 GB) + disk_io × 2 (10 MB, 500 MB) + stdout_firehose + signal_grace + exit_code matrix |
| **Gates** | all_tasks_in("done", expected); duration_within bounds; stdout_tail correct; exit codes match; no_double_claims |
| **Cost** | 1 sbatch worker on amilan, 30 min wall, ~5 min actual compute |
| **Pass criterion** | All Cat A gates pass + report shows clean numbers |

### Tier II — Real binary diversity

| | |
|---|---|
| **Goal** | Each major HPC software package executes via subjob |
| **Pre-conditions** | Tier I passed |
| **What we learn** | Which (if any) binary has stdout/signal quirks that break the runner; which need module-load fixes |
| **Workload** | Cat B: 1 task per binary (LAMMPS + GROMACS + QE + NAMD + PyTorch + numpy) |
| **Gates** | exit_code=0; success marker present in stdout; duration within 0.5–3× expected |
| **Cost** | 1 amilan worker + 1 GPU worker (al40 or aa100). Combined ~60 min wall (including queue) |
| **Pass criterion** | All 6 binaries in done/. Any single failure → halt, remediate (probably a missing `module load`) |

### Tier III — Failure injection

| | |
|---|---|
| **Goal** | Worker classifies + cleans up correctly under adversarial tasks |
| **Pre-conditions** | Tier II passed |
| **What we learn** | Whether subjob can be deployed where users WILL submit bad tasks (almost always) without the worker becoming poisoned |
| **Workload** | Cat C: segfault + oom_kill + sigterm_ignore + infinite_stdout + exit code matrix + slow_start |
| **Gates** | each failure routes to failed/; attempt records have correct exit_code / walltime_killed; worker keeps running through them |
| **Cost** | 1 amilan worker, 30 min |
| **Pass criterion** | All gates pass. If sigterm_ignore doesn't fall through to SIGKILL, that's a real bug, see Remediation §6 |

### Tier IV — Concurrency stress (iterative)

Sub-tiers escalate one variable at a time. Each sub-tier must pass before the next runs.

| Sub-tier | Tasks | Workers | What it adds | Halt-if-fail |
|---|---|---|---|---|
| **IV.a** | 50 | 2 × 1 core | Matches Stage 4 baseline with synthetic load | If this fails, regression vs Phase 0 — investigate immediately |
| **IV.b** | 200 | 4 × 4 cores | 4× more tasks AND 4× more workers AND 4-way internal concurrency | Inspect: which axis broke? |
| **IV.c** | 500 | 4 × 4 cores | 2.5× more tasks at same worker count — tests queue-depth scaling | If only this fails: pool listing or journal write is the bottleneck |
| **IV.d** | 1000 | 8 × 8 cores | Production-like; closest to real per-snapshot fan-out | If this passes, we're ready for dogfood at any reasonable scale |
| **IV.x** | 100 | 2 × 1 with `--exclude` opposing nodes | Forces cross-node race | Validates GPFS cross-node atomicity |

Each sub-tier:
- Gates: `no_double_claims`, `all_tasks_in("done", N)`, `concurrent_at_peak(N × cores)`, `throughput_at_least(baseline×0.5)`
- Cost: amilan; cumulative ~2 hr compute

### Tier V — Recovery + chaos

| | |
|---|---|
| **Goal** | Survival under hostile conditions |
| **Pre-conditions** | Tier IV.b at minimum |
| **What we learn** | Whether subjob recovers from real-world chaos (node kills, user mistakes, walltime) |
| **Workload** | E.1–E.5 |
| **Gates** | Released claims re-claimed; corrupt YAML quarantined; 10k pool listings fast; worker terminates cleanly at SLURM walltime |
| **Cost** | ~40 min |
| **Pass criterion** | All scenarios end with the pool in a consistent state; no abandoned claims; no worker crashes |

### Tier VI — Performance baseline

| | |
|---|---|
| **Goal** | Quantify subjob's overhead. No pass/fail; baseline only. |
| **Pre-conditions** | Tier IV.b passed |
| **What we learn** | What "normal" looks like — needed to detect regressions in future PRs |
| **Workload** | 200 trivial tasks; 1 worker × 8 cores |
| **Outputs** | Throughput, p50/p99 claim latency, journal write rate, submit throughput, pool listing time at 10k |
| **Cost** | 30 min |
| **Pass criterion** | Numbers exist and are saved to `validation/baselines/<date>.md` |

### Tier VII — Multi-partition platforms

| | |
|---|---|
| **Goal** | At least one task succeeds on each partition we use |
| **Pre-conditions** | Tier II passed (for the canonical workload) |
| **What we learn** | Partition-specific quirks (module env, GPU visibility, blanca preemption) |
| **Workload** | LAMMPS LJ on amilan + blanca, PyTorch tiny on al40 + aa100 |
| **Gates** | each task in done/ on its target partition; expected hostname pattern in attempts |
| **Cost** | Variable (blanca queue can be long); 1–6 hr wall |
| **Pass criterion** | Each partition has at least one task in done/ |

### Tier VIII — Production readiness

Models real-deployment hazards that trivial echo/sleep tiers never
exercised. Derived from the pre-deployment audit (items A–L). Each
scenario runs locally by orchestrating worker subprocesses (tests logic,
not cluster scheduling) via `validation/tiers/tier_8_production.py`.

| Scenario | Models | Gates | Status |
|---|---|---|---|
| **VIII.A** SIGTERM preemption mid-task | SLURM preempt / `scancel` of a worker running a long task | claim *released* (not failed) → fresh worker reclaims → completes; worker exits promptly (in-flight subprocess killed, no orphan/double-run) | ✅ 5/5 |
| **VIII.B** max-attempts cap | a task needing more walltime than any worker has | after `retry.max_attempts` releases → moved to `failed/`, doesn't bounce forever | ✅ 3/3 |
| **VIII.C** reap-stale recovery | a worker node dying (no heartbeats in Phase 0) | `subjob reap-stale` returns the orphaned claim to pending → a worker finishes it | ✅ 3/3 |
| **VIII.L** workdir contract | tasks that must run in a specific directory | `Task.workdir` honored by the runner | ✅ 3/3 |

**Code changes this tier drove (commit set 2026-05-28):**
- Worker shutdown now kills in-flight subprocesses and each task thread
  releases its *own* claim (clean preemption — no orphan, no double-run,
  no `executor.shutdown` hang). A pytest
  (`test_worker_sigterm_releases_inflight_task`) covers it.
- `Pool.release()` records a release attempt; worker caps re-claims at
  `retry.max_attempts` (default 3).
- `submit()` uses exclusive create (`os.link`) — concurrent-submitter
  TOCTOU safe.
- New `subjob reap-stale` CLI for manual dead-node recovery.
- `Task.workdir` field + runner `cwd=`.
- Pending cache is mtime-validated (handles release-rewrites safely).

**Still deferred (documented in `docs/DEPLOYMENT.md § 5`):** real GPU
task, single-node MPI task, real research binaries (module chain), and
multi-week pool growth/archival. These have code paths but no real-hardware
end-to-end run yet.

---

## 5. Aggregate pass criteria — "Phase 0 fully validated"

All of:

1. ✅ Tier 0 — harness self-test passes
2. ✅ Tier I — every Category A gate passes
3. ✅ Tier II — all 6 binaries complete in done/ on first attempt (after correct module env)
4. ✅ Tier III — every failure mode classified correctly; worker keeps running
5. ✅ Tier IV.a–IV.d — no double-claims at any scale; throughput scales sub-linearly but doesn't collapse
6. ✅ Tier IV.x — cross-node race exhibits no double-claim
7. ✅ Tier V — all chaos scenarios end in consistent state
8. ✅ Tier VI — baseline numbers recorded; throughput ≥ 5 tasks/sec on trivial workload
9. ✅ Tier VII — each partition has one task in done/

Until all 9 pass, no project-specific (MXene / hydrogenation) workload runs through subjob.

---

## 6. Remediation playbook

For every gate failure class, what to do.

### Atomic claim invariant failure (`no_double_claims` fails)

**Symptom:** Two `task_claimed` events for the same task_id in the journal, OR same task in both `claimed/` and `done/`, OR `claimed/` count > 0 after `pending/` empty.

**Severity:** P0 — corruption of the core architectural promise.

**Diagnosis:**
1. Check the filesystem: `stat -c '%i %n' pool/claimed/* pool/pending/* pool/done/*`
2. Check the journal for `task_claimed` events grouped by `task_id`
3. Confirm pool is on a single filesystem: `stat -f --format='%T' pool/pending pool/claimed` should match
4. Test atomic_move directly: `python -c "from subjob.lib.lock import atomic_move; ..."` on the pool dir

**Remediation:**
- If pool is split across filesystems → `CrossFilesystemError` should have fired; investigate why it didn't
- If GPFS has eventual consistency on rename → this is a major architecture finding; document, escalate, consider O_TMPFILE-based claim instead
- If a single rename returned success twice → kernel bug, escalate to CURC

### Walltime kill doesn't fire (`task_attempt(walltime_killed=True)` fails)

**Symptom:** Task with `walltime_seconds: 2` running `sleep 60` lands in done/ with exit_code=0, or runs past walltime.

**Severity:** P1 — task isolation broken.

**Diagnosis:**
1. Check `runner.py` `Popen.wait(timeout=walltime)` actually fires `TimeoutExpired`
2. Check `_terminate(proc)` sends SIGTERM → SIGKILL fallback
3. Check the task's shell wrapper isn't trapping signals (verify with `strace`)

**Remediation:**
- If subprocess group isn't killed → switch to `os.setsid` + `os.killpg(SIGKILL)` (whole group)
- If shell traps SIGTERM → bash `trap` in command is user error; document as expected, fix Cat C `sigterm_ignore` to test this

### Worker doesn't pick up tasks

**Symptom:** `pending/` has files; worker `worker_started` event in journal; no `task_claimed` events.

**Diagnosis:**
1. Inspect peeked task: cores/gpus required vs worker capabilities (Tier IV.d may surface this)
2. Check walltime fit: `remaining_walltime ≥ task.walltime_seconds`
3. Check pool root permissions (worker user vs file owner)

**Remediation:**
- If capability mismatch → user error in worker sbatch; document expected workflow
- If walltime gating is too aggressive → tune `walltime_safety_s` default (currently 60 s)

### Real binary fails to start (Tier II)

**Symptom:** Task in failed/, exit_code 127 (command not found) or 1 with "module not found".

**Diagnosis:**
1. Check the rendered sbatch script has the correct `module load` lines
2. Check the binary's actual path on Alpine
3. Check the binary is installed at all (`module spider <binary>`)

**Remediation:**
- Update the task template to load the right module before invoking the binary
- Per START_HERE: subjob does NOT manage conda envs. The task command is responsible for `module load X` itself

### Journal events missing

**Symptom:** Gate `journal_event_present` fails despite task being in done/.

**Diagnosis:**
1. Check `pool/journal.jsonl` raw — is the event line there?
2. Check `read_journal()` `since_event_id` filtering
3. Check for partial writes (line not terminated by `\n`)

**Remediation:**
- Partial writes shouldn't happen since `open('a').write(json + '\n')` is one syscall — investigate concurrent writers
- Add `flock(LOCK_EX)` around journal writes if multiple workers truly do contend (would be a Phase 0 oversight)

### Performance regression

**Symptom:** Tier VI throughput drops 2× vs `baselines/<previous>.md`.

**Diagnosis:**
1. Compare `baselines/<previous>.md` to `baselines/<current>.md` field-by-field
2. Run `cProfile` on `pool.pending_paths()` — most likely culprit
3. Check `git log` since the last baseline

**Remediation:**
- Don't merge the offending PR until baseline restored OR the regression has a justification
- If GPFS metadata performance dropped → it's CURC infrastructure, not us; document and move on

### "Phase 0 anti-feature" appears needed

**Symptom:** A gate keeps failing and the only way to make it pass is to build something on the anti-feature list (DAG, GPU accounting, heartbeats, etc.).

**Severity:** Architectural.

**Remediation:**
- Stop. Surface to Sean. Reconfirm whether this is a Phase 0 expectation or a Phase 1 trigger.
- Per START_HERE §12: if Phase 0 anti-feature is actually needed, that's a rare-but-possible flag.

---

## 7. Execution sequence (calendar-free; gate-driven)

```
build harness (Tier 0)
       │
       ▼
Tier I synthetic     ─── fails ───▶ remediate (see § Remediation)
       │ pass
       ▼
Tier II real binaries ─── fails ──▶ usually module env; update template
       │ pass
       ▼
Tier III failure inj ─── fails ───▶ subjob bug; halt, fix, retest
       │ pass
       ▼
Tier IV.a (50, 2×1)
       │ pass
       ▼
Tier IV.b (200, 4×4)
       │ pass
       ▼
Tier IV.c (500, 4×4)
       │ pass
       ▼
Tier IV.d (1000, 8×8)
       │ pass
       ▼
Tier IV.x (cross-node)
       │ pass
       ▼
Tier V recovery
       │ pass
       ▼
Tier VI perf baseline (saves baseline)
       │
       ▼
Tier VII platforms
       │ all pass
       ▼
PHASE 0 FULLY VALIDATED — green-light for project dogfood
```

Estimated total compute: ~4 hr across all tiers (most is short; bulk is in Tier IV.d and Tier VII blanca queue wait).

---

## 8. Risk register

What could still surprise us, ordered by likelihood × impact:

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Real binary modules differ from local Python — wrong `module load` | High | Low (easy fix) | Tier II templates pin specific module versions |
| GPFS metadata caching causes apparent atomic-rename failure | Low | P0 | Tier IV.x explicitly tests cross-node; Tier 0 sanity tests atomic_move |
| SLURM SIGTERM-at-walltime arrives during a journal write | Low | Medium (truncated event) | Tier V.4 exercises this; mitigation: open(append) is one syscall, but verify |
| 10 000-task pool dir slow on GPFS | Medium | Medium (UX) | Tier V.3 measures; if slow, shard pending/ by hash prefix |
| PyTorch GPU detection differs amilan vs al40 | Medium | Low (per-task command issue) | Tier II + Tier VII surface this; fix in template |
| Worker thread crash in `_run_one` → claim stranded | Low | Medium (one task lost) | Tier 0 tests this via an exception-raising mock; remediation: `_reap_finished` already catches and commits as failed |
| Phase 0 anti-feature genuinely needed mid-test | Low | High (scope creep) | Documented escalation path in § Remediation |

## 9. Findings from validation runs

### F-001 — Pool listing dominates throughput at 5k+ tasks  (2026-05-27, Tier V.E3)

**Symptom:** With 5000 tasks pending and one 8-core worker, sustained
throughput drops from ~9 tasks/s (Tier IV.b, 200 tasks) to ~2.4 tasks/s.
Worker hit its 30-min walltime with 882/5000 tasks still pending.

**Root cause:** `Pool.pending_paths()` reads every YAML in pending/ on
every poll cycle to extract priority for sorting. With 5000 files in
pending/, each poll cycle parses 5000 YAML files. At ~1 ms per parse,
that's ~5 s per poll — and a poll happens every time the worker reaps a
finished task, so the cost is paid per-completion.

**Phase-0 status:** Not a blocker for the per-snapshot dogfood workload
(60 tasks). Documented as a known limitation.

**Phase-1 fix candidates:**
  - Encode priority in the filename (e.g., `priority_NNN/task_id.yaml`)
    so directory listing yields priority without read.
  - Cache `(filename → priority)` map and invalidate only when
    `pending/` mtime changes.
  - Bucket pending/ by priority subdirs.

**Cure:** Either of the above. None affect the public API.

### F-002 — Claim-latency metric in Tier VI confounded by queue wait  (2026-05-27)

Tier VI reports claim latency p50=253s. This is `task_claimed.timestamp - task_submitted.timestamp` — but the worker spent ~4 min in the SLURM queue before running. The metric mixes "subjob is slow to claim" and "SLURM queue is busy" into one number.

**Future:** add a Tier VI gate using `(task_claimed - worker_started)` as the latency baseline.

---

## 9. Conventions & how to add new tests

### Adding a new synthetic workload
1. Add a function to `workloads.py`: `def make_<name>(id: str, **params) -> Task`
2. Add a gate or duration bound expectation
3. Reference it from a tier in `tiers/tier_<N>_<name>.py`

### Adding a new real binary
1. Place public input under `inputs/<binary>/`
2. Create `binaries/<binary>.yaml.tmpl` with `{POOL}` / `{INPUT_DIR}` placeholders
3. Add a row to the Tier II table above
4. Update Tier VII if it should run on a specific partition

### Pool dir convention
All validation pools under `/scratch/alpine/sefl7948/pools/subjob-val-<tier>-<timestamp>/`. Cleaned up by `validation/cleanup.sh` (also under harness).

### Reports
Each tier writes `<pool>/REPORT.md`. Aggregated reports go to `validation/reports/<date>/<tier>.md`. Baselines (Tier VI) go to `validation/baselines/<date>.md`.

---

## 10. Open questions to resolve before / during execution

- **Modules on Alpine** — what's the exact `module load` line for each binary in Tier II? (Resolve during Tier II setup; pin versions in templates.)
- **GPU partition policy** — do we use al40 (RTX 8000) or aa100 (A100) for PyTorch test? Either works; pick the less-busy queue at test time.
- **Blanca preemption** — Tier VII blanca task may get preempted. Need to verify the worker's release-on-shutdown behavior handles this. (Mitigation: Tier V.4 already covers it; Blanca preemption is just a real-world SIGTERM.)
- **Where does the validation suite live long-term?** — under `validation/` for now; may extract to a separate `subjob-validation` package if it grows.

---

*End of plan. See `validation/PLAN.md` for the operational doc; `docs/VALIDATION_PLAN.md` is the short pointer.*
