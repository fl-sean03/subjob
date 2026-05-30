# Session log — pre-dogfood Phase-1 infrastructure (2026-05-30)

> Durable session log per `~/.claude/skills/agent-orchestration/SKILL.md` §
> "Special case: maximally-broad user grant." Append-only event stream +
> decisions log so the user can audit a night's autonomous work cold.

## Authority grant

**Verbatim from Sean (2026-05-30):**
> *"oaky lets go ahead and do all this, deploy sibagent to go ahead and
> take ofer full ened to end developemnt, read the orchestration agent
> skill, from here on out you have full autonomy"*

**Scope (echoed back at session start):** root orchestrator may auto-cross
previously-human-gated phase boundaries (Thrusts 8–11 pulling Phase-1
infrastructure forward — artifact validation, DAG enforcement, heartbeats
+ auto stale-claim recovery, priors framework + diagnose — plus their
reviews, Cycle 2 re-audit, and closeout); may make best-informed decisions
on novel questions; may spawn specialized subordinates as needed.

**Operating layer:** `docs/AUTONOMOUS_DEV_LOOP.md` (the project's ADL)
runs as the operational protocol within this broad grant.

## Locked safety conventions (survive the broad grant)

These remain forbidden even under "full autonomy":

- **Destructive git ops against `main`** — PR #1 from `phase0-mvp` is the
  working branch; no force-push to main, no history rewrite of merged
  commits.
- **Phase-2 anti-features stay deferred:** multi-GPU type accounting, CCM
  / Vast.ai cloud backend, cost-aware backend selection. Each needs a real
  workload to design well; pulling them forward = the speculation trap
  the anti-feature list exists to prevent.
- **No Anthropic API usage** in subjob code; this is stdlib-only at
  runtime by design.
- **No skill self-modification** beyond appending to its own changelog.
- **Documented acceptance criteria not relaxed:** the ADL's "review
  until converged (0 P0/P1)" invariant holds. The root orchestrator may
  proxy as reviewer at thrust convergence points but cannot lower the bar.
- **No destructive Alpine `/scratch` ops** without confirming cleanup.
- **Anti-feature scope-creep guard still applies recursively:** if a
  subordinate's audit surfaces a temptation to build a Phase-2 feature,
  the answer is "log it, don't act" — not "since we're pulling Phase 1
  forward, Phase 2 is fair game too."

## Authorized pull-forwards (Phase 1 features explicitly granted this window)

Per user direction (2026-05-30 audit conversation), these are no longer
gated on the hydrogenation cool+prod dogfood:

- Artifact validation (`expect`, `success_marker`) — Thrust 8.
- DAG enforcement (`depends_on`) — Thrust 9 (officially Phase 2 per
  ARCHITECTURE; user explicitly authorized pull-forward as general infra).
- Heartbeats + auto stale-claim recovery — Thrust 10.
- Priors framework + `pool.diagnose()` + `subjob diagnose` CLI — Thrust
  11 (framework + matcher; specific lab priors catalog still deferred to
  real failures).

## Open subordinate registry

| Agent ID | Brief | Lane | Status |
|---|---|---|---|
| a4641c57a2d77f941 | Thrust 8 — artifact validation | `src/subjob/lib/artifacts.py` (new), `worker.py`, `cli.py`, tests, AGENT_GUIDE | running |

## Decisions log

### D-1 — 2026-05-30 — Pull DAG enforcement forward (officially Phase 2)
- **Decision:** include DAG/`depends_on` enforcement in this pre-dogfood
  infrastructure push (Thrust 9) even though ARCHITECTURE marks it Phase 2.
- **Rationale:** the user explicitly cited DAG as exactly the kind of
  workload-independent infrastructure worth building now. Schema is
  already parsed; semantics are unambiguous (dependents wait for `done/`).
  Builds on the staged-sweep pattern already in AGENT_GUIDE.
- **Reversibility:** high — if it surfaces an interaction we don't like,
  revert by removing the dep-check in `_dispatch_pending` (one block).

### D-2 — 2026-05-30 — Defer the rest of Phase 2
- **Decision:** multi-GPU type accounting, CCM cloud backend, cost-aware
  backend selection stay deferred per the original anti-feature reasoning.
- **Rationale:** these are workload-dependent (need heterogeneous-GPU
  workloads / spot-resilience need / multi-backend cost data to design).
  Building them now without a workload IS the speculation trap.
- **Reversibility:** n/a — declining to build is reversible by definition.

### D-3 — 2026-05-30 — Run thrusts sequentially, not parallel implementers
- **Decision:** Thrusts 8–11 run as a sequence (each implement → verify →
  review → next), not parallel implementer subordinates.
- **Rationale:** all four touch `pool.py`/`worker.py`/`cli.py`. Parallel
  implementers would conflict (skill §"File-lane collisions"). A single
  implementer per thrust with clean file lanes prevents the failure mode.
- **Reversibility:** trivial — can reorder thrusts if findings change.

### D-5 — 2026-05-30 — Verify-and-commit per thrust; single Cycle-2 audit at end
- **Decision:** orchestrator verifies each implementer's diff + tests + ruff
  itself + commits, then a single Cycle-2 re-audit at the end (2 parallel
  auditors) covers all four thrusts collectively, rather than per-thrust
  reviewer subordinates.
- **Rationale:** the ADL inner loop allows the principal to act as proxy
  reviewer under broad authority; the Cycle-2 audit provides independent
  P0/P1 eyes across the whole batch. Per-thrust reviewer subordinates
  would add ~4 round-trips of agent overhead for marginal additional
  rigor when the audit catches the same class of issues.
- **Reversibility:** if Cycle 2 surfaces material issues, treat them as a
  normal post-cycle fix thrust (same pattern as Cycle 1 → Thrust 4).

### D-4 — 2026-05-30 — Cycle 2 re-audit closes the window
- **Decision:** after Thrusts 8–11 land, run a full Cycle 2 re-audit
  (2 parallel auditors). Surface a consolidated health summary to the
  user at that boundary; do not auto-roll into a Cycle 3.
- **Rationale:** matches ADL's "cycle boundary surface" trigger. The user
  asked for the pre-dogfood infrastructure to land; once Cycle 2 converges
  there's no further mandate to keep churning.
- **Reversibility:** the user can direct continuation in the morning.

## Event stream (append-only)

- 2026-05-30 — Session opened. Authority grant echoed. Skill loaded.
- 2026-05-30 — Pre-existing tasks #53–57 created for Thrusts 8–11 + Cycle 2 re-audit.
- 2026-05-30 — Thrust 8 (artifact validation) subordinate `a4641c57a2d77f941` spawned BEFORE this session log existed; brief is the version captured in that Agent call. Future briefs (Thrusts 9–11 + audits) will follow the eleven-section skeleton from the skill, including this skill's path in required reading.
- 2026-05-30 — Thrust 8 returned: 159 tests pass, ruff clean. Diff verified by orchestrator (worker happy-path gate is correct; shutdown/release paths untouched). Committed `1e2498c` → pushed.
- 2026-05-30 — Thrust 9 (DAG enforcement) subordinate `a5ac031a0b4d115e1` spawned with the full eleven-section brief. Lane: `src/subjob/worker/worker.py`, `docs/AGENT_GUIDE.md`, `tests/test_worker.py`. Required reading includes the orchestration skill so the subordinate auto-loads it if it spawns further subordinates (brief explicitly says it should NOT need to).
- 2026-05-30 — Thrust 9 returned: 165 tests pass (+6), ruff clean, hypothesis held (per-task pre-claim gate with lazily-cached state-id sets was sufficient; no graph datastructure, no Pool changes, no new event type). Diff verified by orchestrator (DAG block sits between max_attempts cap and cores/gpus gate; AGENT_GUIDE banner removed + example rewritten + table updated). Committed `68aad95` → pushed.
- 2026-05-30 — Thrust 10 (heartbeats + auto stale-claim recovery) subordinate spawned with full eleven-section brief. Lane: `src/subjob/worker/worker.py`, `src/subjob/lib/pool.py` (claim-time owner stamp), `src/subjob/client/cli.py` (`reap-stale --auto`), tests, AGENT_GUIDE.
- 2026-05-30 — Thrust 10 returned: 174 tests pass (+9), ruff clean, hypothesis held (heartbeat file + claim-stamp + per-poll-interval auto-sweep = self-healing cohort, no daemon). Three flagged deviations all accepted by orchestrator: (a) Pool gained 4 helpers + `auto_reap_stale` — justified because both Worker and CLI need the underlying recovery primitive; (b) `--older-than` is no longer required when `--auto` (default 120s) — matches the brief; (c) `worker_started` event payload gained `worker_id` (additive, safe). Dead-worker → live-worker recovery measured at ~50ms wall. Committed `335b3ed` → pushed.

### D-6 — 2026-05-30 — Accept Thrust-10 deviation: Pool gains substantial new methods

- **Decision:** keep `Pool.auto_reap_stale` and the heartbeat trio as Pool methods rather than inlining or extracting to a free function.
- **Rationale:** the brief invited the surface (`if you find that a substantial Pool method would dramatically simplify things, document and surface it`). Both Worker (`_maybe_auto_reap`) and CLI (`reap-stale --auto`) consume the same per-task heartbeat logic; a free function would need either deep Pool internal access or argument-passing of half-a-dozen fields. The heartbeat ops are tiny atomic file ops that naturally belong with Pool's other file ops (mirrors `_stage_write` / journal flock locality).
- **Reversibility:** trivial — the helpers could be relocated later if Pool grows too wide. The interface is already minimal: 3 heartbeat ops + 1 auto_reap_stale + 1 worker_id init param.

- 2026-05-30 — Thrust 11 (priors framework + diagnose) subordinate spawned with full eleven-section brief. Lane: NEW `src/subjob/lib/priors.py`, `src/subjob/lib/pool.py` (diagnose method), `src/subjob/client/cli.py` (diagnose subcommand), tests, AGENT_GUIDE.
- 2026-05-30 — Thrust 11 returned: 203 tests pass (+29), ruff clean, hypothesis held (advisory-only framework was sufficient; no worker-time hooks needed for Phase 1). One deviation accepted: `priors_apply` (the Task author's declared list) is surfaced in the diagnose result for operator visibility, even though the worker remains inert on it. Edge case noted: a prior constrained on `exit_code` won't match artifact-validation failures (which have `exit_code: None`); catch-all priors still catch them. Committed `bb264ae` → pushed.
- 2026-05-30 — **All 4 pre-dogfood Phase-1 thrusts landed.** 203 tests, ruff clean. Commits: `1e2498c` (artifact validation), `68aad95` (DAG), `335b3ed` (heartbeats + auto-recovery), `bb264ae` (priors + diagnose). All session-log decisions D-1..D-6 followed; no Phase-2 anti-features were touched.
- 2026-05-30 — Cycle 2 re-audit spawned: two parallel auditors over the whole platform per D-4. Auditor A: core correctness/concurrency (pool/worker/runner/lock/task/yaml_lite/priors/artifacts). Auditor B: interfaces/docs/tests/scope. Convergence verdict closes the autonomous window.
- 2026-05-30 — Cycle 2 audit returned: **1 P0 (A), 9 P1 (2 from A + 7 from B), 10 P2, 5 P3.** Scope discipline clean per both.
  - **P0 (A):** `_finalize` / `release` / `auto_reap_stale` use `exists()`-check-then-`write_text`. T10's auto-reap opened a cross-worker write race: A's finalize passes `.exists()` → B's reaper renames the file to pending/ → A's `write_text` re-creates the file in `claimed/` and renames to `done/`. Same task lives in two dirs → double-claim. The Cycle-1 "first finalize wins" guard is necessary but not sufficient under T10 cross-worker competition.
  - **P1 (A):** yaml_lite silently drops unknown backslash escapes (`"\d+"` → `"d+"`); DAG `unknown_dep` races concurrent submitters.
  - **P1 (B):** artifacts `$VAR` expansion falls back to `os.environ` despite docs claiming task-env-only; README/DEPLOYMENT/ARCHITECTURE/START_HERE/RESULTS/AGENT_GUIDE all still list T8–T11 features as "Phase 1 / not yet implemented" (6 stale callouts); `Pool.diagnose` raises `PriorSchemaError` uncaught (CLI catches but docs Python example doesn't).
  - **P2/P3:** small worker_id collision space, no priors match key for artifact_validation_failed, ARCHITECTURE worker pseudocode references non-existent Pool methods, several test-coverage gaps for interaction paths.

### D-7 — 2026-05-30 — Two fix thrusts; T12 (code) first, T13 (docs) in parallel after T12 brief committed

- **Decision:** Thrust 12 takes P0 + 3 P1 code fixes (pool/yaml_lite/artifacts/worker — single file lane in `src/`). Thrust 13 takes the 6 doc-staleness P1s + P2 doc polish (docs only — disjoint file lane). Both run sequentially with T12 first since the P0 must land before any further validation.
- **Rationale:** the P0 fix must land before the doc sweep so RESULTS.md can claim "0 P0/P1" honestly when the cycle records as converged. The two thrusts have zero file-lane overlap so technically they could parallelize, but T12 is the load-bearing correctness fix and deserves orchestrator attention end-to-end without splitting between two returning subordinates.
- **Reversibility:** trivial — both thrusts are tightly bounded.

- 2026-05-30 — Thrust 12 (P0 + 3 P1 code) subordinate spawned. Lane: `src/subjob/lib/{pool,yaml_lite,artifacts}.py` + `src/subjob/worker/worker.py` + tests.
- 2026-05-30 — Thrust 12 returned: 212 tests pass (+9), ruff clean, rename-first hypothesis held, manual 50/50 threaded race smoke confirms no resurrection. P0 fix verified by orchestrator (`_finalize` + `release` both now: exists()→rename→write_text-at-dest→emit; the load-bearing exclusion is the atomic rename, not the .exists() guard). Committed `64af868` → pushed.
- 2026-05-30 — Thrust 13 (doc-staleness sweep) subordinate spawned. Lane: `README.md`, `START_HERE.md`, `docs/{ARCHITECTURE,DEPLOYMENT,AGENT_GUIDE}.md`, `validation/RESULTS.md` + small Pool.diagnose change to catch PriorSchemaError internally. T12 already touched AGENT_GUIDE's DAG section narrowly — T13 owns the rest of the sweep with explicit instruction to leave the DAG grace note in place.
- 2026-05-30 — Thrust 13 returned: 214 tests (+2), ruff clean, all 6 P1 doc-staleness items + Pool.diagnose internal catch + cli text-format error path all done. Grep audit: 4 remaining hits all intentional (Planned section header, ccm Phase-2 marker, historical Phase-0 don't-build list, historical Cycle-1 narrative). Committed `23d5cc4` → pushed.
- 2026-05-30 — Cycle 2 final convergence audit spawned. Lighter brief (one focused auditor): verify rename-first P0 fix holds across all three call sites; verify the doc-staleness sweep is clean; broad P0/P1 sweep. CONVERGED (0 P0/P1) closes Cycle 2 → morning summary surfaces to user per ADL + orchestration skill.
