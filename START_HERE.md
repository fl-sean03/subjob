# START HERE — subjob project handoff

> **Read this whole file first.** This is your onboarding. You are picking up Phase 0 of a project that has design done and zero code written. Your job: build the MVP. Don't redesign — the architecture is locked. Don't expand scope — the dogfood use case is locked. **Build what's specified, ship it small, iterate based on real use.**

---

## 1. What this project is (60 seconds)

`subjob` is a **pilot job scheduler** for HPC. Submit one big SLURM allocation; inside it, a worker process pulls tasks from a shared filesystem-based pool and runs them concurrently as cores free up. Many short tasks share one queue wait.

**Target users**: agents (primary) and humans (secondary), running scientific compute in the Heinz lab at CU Boulder, currently on the CURC Alpine cluster.

**Not a generic workflow engine.** Not a database-backed broker. Not a cloud orchestrator. Not a daemon-style service. It's: a directory of task YAMLs, a worker process that drains them, a small CLI.

The seed idea came from [`fl-sean03/allocation-scheduler`](https://github.com/fl-sean03/allocation-scheduler); we diverged on architecture (multi-pilot via shared FS instead of single SQLite-per-allocation).

---

## 2. Read these, in this order

1. **`README.md`** (this repo) — 2 min. What it is + where it sits in the lab tooling landscape.
2. **`docs/ARCHITECTURE.md`** — 10 min. The full design: pool layout, task spec, worker lifecycle, backend abstraction, what's NOT in scope. **Don't change this without an explicit decision record update.**
3. **`docs/DEVELOPMENT.md`** — 5 min. The phase plan. You are doing **Phase 0** only. Phases 1+ are future work.
4. **`docs/AGENT_GUIDE.md`** — 5 min. How the API + CLI is supposed to feel from the agent's perspective. **This is what you're building toward.**
5. **`docs/CCM_INTEGRATION_ANALYSIS.md`** — 5 min. Why this project is separate from `~/Workspace/main/46-CCM/` and how they integrate later (Phase 2).
6. *(Optional)* `~/Workspace/main/46-CCM/AGENTS.md` — context on the sister lab project. Skim only.
7. *(Optional)* Skim the upstream seed at `https://github.com/fl-sean03/allocation-scheduler` to understand what we're departing from.

After this you should be able to answer:
- What's the canonical task spec? (YAML, see ARCHITECTURE.md § "Task spec")
- How do workers claim tasks? (Atomic FS rename `pending/X.yaml` → `claimed/X.yaml`)
- Where does the journal live? (`<pool>/journal.jsonl`, append-only)
- What's the dogfood use case for Phase 1? (Per-snapshot analysis: 60 short G1+G2 + Y.20 tasks against the hydrogenation cool+prod outputs)
- What backends are in scope for Phase 0? (Only `slurm`)

---

## 3. Phase 0 scope (exactly what to build)

**Do build:**

- `src/subjob/lib/task.py` — `Task` dataclass matching the YAML spec in ARCHITECTURE.md
- `src/subjob/lib/pool.py` — `Pool` class with: `submit()`, `submit_batch()`, `status()`, `follow()`, `claim()`, `release()`, `commit()`. Directory layout: `<pool>/{pending,claimed,done,failed}/<id>.yaml` plus `<pool>/journal.jsonl`.
- `src/subjob/lib/lock.py` — atomic rename-based claim primitive. **Use `os.rename` (atomic on POSIX same-filesystem) for claim. Don't use lockfiles, flock, or FS locks — they're flaky on shared FS.**
- `src/subjob/worker/worker.py` — poll loop, claim tasks, exec command, emit journal events, release on shutdown.
- `src/subjob/worker/runner.py` — execute one task: spawn subprocess, capture stdout/stderr to per-task log file, enforce walltime, return ExitResult.
- `src/subjob/client/cli.py` — entry point with subcommands: `submit`, `status`, `follow`, `cancel`. Argparse, JSON output by default.
- `src/subjob/backends/slurm.py` — minimal: write an sbatch wrapper that runs `python -m subjob.worker --pool <dir>` and submits it. Returns the SLURM job id.
- `examples/` — 2 working examples:
  - `examples/sleep_test.py` — submits 5 trivial sleep tasks. Validates pool + worker round-trip.
  - `examples/per_snapshot_analysis_dryrun.py` — generates task YAMLs matching the Phase 1 dogfood workload. Doesn't run them; just produces the YAML for inspection.
- `tests/test_pool.py`, `tests/test_worker.py`, `tests/test_claim_race.py` — pytest, no compute, < 10 sec each. Race test should spawn 4 fake workers and verify exactly one wins each task.
- `pyproject.toml` — minimal, Python ≥ 3.9, stdlib only (no external runtime deps). Dev deps: pytest, ruff.

**Don't build (the original Phase 0 contract — kept here as historical
scope discipline; see README.md and `validation/RESULTS.md` for what's
actually shipped today):**

- ~~Task DAG (`depends_on`)~~ — **shipped 2026-05-30** (Cycle 2 Thrust 9; commit `68aad95`)
- ❌ GPU resource tracking (multi-GPU type / per-device accounting) — Phase 2
- ~~Priors integration / failure classification~~ — **shipped 2026-05-30** (Cycle 2 Thrust 11; commit `bb264ae` — `pool.diagnose` + `subjob diagnose`)
- ~~Artifact validation~~ — **shipped 2026-05-30** (Cycle 2 Thrust 8; commit `1e2498c` — `artifacts.expect` + `success_marker`)
- ❌ Cancellation API — Phase 1 (until someone asks)
- ~~Multi-pilot heartbeats / stale-lock recovery~~ — **shipped 2026-05-30** (Cycle 2 Thrust 10; commit `335b3ed` — heartbeats + auto reap-stale)
- ❌ Web UI / TUI — Phase 3
- ❌ CCM backend — Phase 2
- ❌ Database — never
- ❌ Authentication — never (in our scope)
- ❌ Anything not listed in the "Do build" list

If you find yourself adding "just a little of X" where X above is still
`❌`, **stop and ask the user**. The struck-through rows above shipped in
Cycle 2 (2026-05-30) under explicit user direction — see
`docs/sessions/2026-05-30_pre-dogfood-phase1-infra.md` for the authority
chain. New work past those needs the same explicit gate.

---

## 4. Implementation guidance

### Stack constraints

- **Python 3.9+, standard library only** at runtime. Same constraint as upstream. Justification: HPC environments without conda need to run this. Pytest + ruff are dev-only.
- **No type-checker dependency.** Use type hints generously; don't add mypy as a runtime requirement.
- **Logging via stdlib `logging` module.** No `loguru`, no `rich`. Configure once at CLI entry.
- **YAML via stdlib?** Python has no stdlib YAML. Options:
  1. Use stdlib `tomllib` for the task spec instead of YAML — TOML in 3.11+.
  2. Use a tiny hand-rolled YAML subset parser (we only need flat key:value + nested maps + lists of dicts — no anchors/aliases/multidoc).
  3. Accept a `PyYAML` dependency. The upstream seed uses pure-stdlib; we should too if possible.

   **Recommendation: hand-rolled minimal-YAML parser** in `lib/yaml_lite.py` (~80 lines). Document the supported subset clearly. Reject unsupported features explicitly. If the user really wants full YAML later, we can swap in PyYAML.

### File / directory conventions

- Pool root must be on shared FS (`/scratch/...` on Alpine).
- Task IDs are user-chosen strings — validate they're safe filenames (`[A-Za-z0-9_.-]+`, ≤ 200 chars).
- Journal entries are one JSON object per line, fields: `{timestamp, event_id, type, task_id, payload}`. Event IDs monotonically increasing (just `time.time_ns()` is fine for now).
- Per-task log files at `<pool>/logs/<task_id>.out` and `.err`. Don't reuse SLURM's stdout — workers append per-task.

### Atomic claim — the critical primitive

```python
def claim(self, task_path: Path) -> Path | None:
    """Atomically move pending/<id>.yaml → claimed/<id>.yaml. Returns new path or None if lost the race."""
    target = self.claimed_dir / task_path.name
    try:
        # os.rename is atomic on POSIX same-filesystem. EXDEV otherwise.
        os.rename(task_path, target)
        return target
    except FileNotFoundError:
        # Another worker beat us. Normal.
        return None
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise RuntimeError("Pool must be on a single filesystem") from e
        raise
```

This is the entire concurrency story. There's no database, no lock files, no daemons. If it works for `mv` between two dirs on the same filesystem, it works for us.

### Worker walltime awareness

The worker needs to know how much time it has left. Inside SLURM, parse `$SLURM_JOB_END_TIME` or compute from `SLURM_JOB_START_TIME + SLURM_JOB_TIMELIMIT`. Outside SLURM (local backend), read a `--walltime-seconds` CLI flag.

Don't start a new task if remaining time < task's declared walltime. Release unfinished claims on shutdown.

---

## 5. Testing approach

| Test file | What it covers |
|---|---|
| `tests/test_task.py` | Task serialization round-trip (YAML → Task → YAML). Validation of IDs. |
| `tests/test_pool.py` | Submit, list pending, status counts. Use `tmp_path` fixture. |
| `tests/test_claim_race.py` | Spawn 4 `multiprocessing.Process` workers. Submit 10 tasks. Verify all 10 claimed exactly once. |
| `tests/test_worker.py` | Run worker against 5 sleep tasks. Verify they all complete in `done/`. Verify journal has expected events. |
| `tests/test_cli.py` | Invoke CLI subcommands via subprocess. Check JSON output structure. |

Target: 30 tests, all under 10 seconds total. No SLURM dependency in tests — use local backend for everything.

---

## 6. The Phase 1 dogfood workload (write tests against this in mind)

When Phase 0 ships, the next thing we do is run it against this real workload:

```
Pool: /scratch/alpine/sefl7948/pools/hydrog-analysis-<date>

Tasks (60 total):
- Pt100-snap-001 through Pt100-snap-020:
    command: /hydrog-run-pipeline G1G2-slab-pt100-... --snapshot N
    cores: 4
    walltime: 1800
- Pt111-snap-001 through Pt111-snap-020: same pattern
- Pt110-snap-001 through Pt110-snap-020: same pattern

Worker: 1× amilan c64 long QoS, 7-day walltime
Expected throughput: 60 tasks × ~5 min each = 5 hr of compute total
Wall time with 16-concurrent c4 tasks: ~30 min after dispatch
```

If your MVP can run this without code changes, Phase 0 is done.

---

## 7. Decisions already made — do NOT re-litigate

The following were debated and decided. If you find yourself wanting to revisit, **ask the user first**.

| Decision | Rationale |
|---|---|
| Filesystem-as-broker (not DB, not Redis) | HPC reality; works everywhere; no daemon politics |
| YAML task files (not JSON) | Human readable; agents can write directly |
| Multi-pilot (not single-allocation like upstream) | Sean's lab runs multiple partitions/clusters |
| Project name = `subjob` | Sean picked from 10 options |
| Python stdlib only at runtime | Same as upstream; works in any HPC env |
| Don't fold in CCM | They solve different problems; see CCM_INTEGRATION_ANALYSIS.md |
| Skip DAG/GPU/priors in Phase 0 | Dogfood-driven feature addition; don't speculate |
| Name "subjob" | Already debated — keeper |

---

## 8. Anti-feature list (resist temptation)

Don't build these unless 3 separate real workloads explicitly need them:

- Web UI
- Database backend
- Authentication / multi-tenancy
- Workflow DSL
- Auto-scaling worker count
- Cluster federation
- Real-time pub-sub (polling is fine)
- Caching layer
- Plugin system beyond backend interface

Stay small.

---

## 9. Environment + tooling

- **Working dir**: `~/Workspace/main/47-subjob/` on Sean's WSL2 machine
- **Python**: use `python3` (system); user has miniconda but stdlib-only means we don't need an env
- **Test runner**: `pytest`
- **Linter**: `ruff` with default config (add `pyproject.toml` `[tool.ruff]` section)
- **Git**: branch from `main`, commit early/often
- **Commit message style**: imperative subject line, body explains why, end with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>` if you used Claude

---

## 10. Connections to other lab projects

If you need context on what's going on around this:

- `~/LabWork/Workspace/31-Hydrogenation/` — the project currently driving Phase 1 dogfood (Pt-NEC MD ensembles)
- `~/Workspace/main/46-CCM/` — sister project (cloud compute mgr), separate but will integrate at Phase 2
- `~/LabWork/Workspace/29-AgenticScienceWorker/1-ScienceAgent/skills/compute-strategy/` — general framework skill (cross-project)
- `~/LabWork/Workspace/31-Hydrogenation/simulations/.priors.yaml` — example of the kind of failure-catalog file we'll integrate with later (Phase 1)
- `~/LabWork/Workspace/31-Hydrogenation/hpc/COMPUTE_STRATEGY.md` — empirical Alpine routing data this project will eventually consume

You don't need to read these to do Phase 0. You will need them by Phase 1.

---

## 11. Tactical first steps (the next 30 minutes)

When you start:

1. `cd ~/Workspace/main/47-subjob && git checkout -b phase0-mvp`
2. Read all 4 doc files listed in §2.
3. Write `pyproject.toml` first, with the right Python version + dev-only deps. Commit.
4. Write `src/subjob/lib/task.py` (the Task dataclass + YAML round-trip). Add `tests/test_task.py`. Commit.
5. Write `src/subjob/lib/lock.py` (atomic claim). Add `tests/test_claim_race.py`. Commit.
6. Write `src/subjob/lib/pool.py` (the Pool API). Add `tests/test_pool.py`. Commit.
7. Write `src/subjob/worker/runner.py` then `worker.py`. Add `tests/test_worker.py`. Commit.
8. Write CLI + slurm backend last. Sanity-check with `examples/sleep_test.py`. Commit.
9. Document `examples/per_snapshot_analysis_dryrun.py` as the canonical Phase 1 input. Commit.
10. Push to a remote (if Sean sets one up) and write a short summary message tagging `@sean` with: features delivered, tests passing, demo command to run.

**Don't write big bang.** Write the smallest thing, test it, commit, move on.

---

## 12. When to stop and ask

Stop and surface to the user if:

- You're considering changing anything in `docs/ARCHITECTURE.md` § "Decision rationale" or § "What's NOT in scope"
- You find yourself writing more than 200 LOC of "infrastructure" code (caches, registries, plugin loaders)
- A test you can't make pass with simple FS primitives — there might be an architectural problem
- You discover that a Phase 0 anti-feature is actually needed (rare but possible)
- You're adding a runtime dependency outside the stdlib
- The MVP is more than 2000 LOC total (target: < 1500 LOC)

---

## 13. Definition of done — Phase 0

- All Phase 0 deliverables in §3 exist
- All tests in §5 pass
- `examples/sleep_test.py` works end-to-end (submit, worker runs, all 5 tasks done)
- `examples/per_snapshot_analysis_dryrun.py` produces valid task YAMLs for the Phase 1 workload
- README updated with quickstart (one paragraph + one code block)
- A 5-line "what's next" section appended to `docs/DEVELOPMENT.md` Phase 1 with "Phase 0 shipped on <date>, Phase 1 begins when cool+prod fan-out completes on `~/LabWork/Workspace/31-Hydrogenation/`"

When all of those are checked: ping Sean. Phase 0 is done.

---

Good luck. Build small, ship fast.
