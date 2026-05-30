# Autonomous Development Loop (ADL)

How the orchestrator (the driving Claude session) advances `subjob` without
needing a human prompt each cycle. This is the process the orchestrator
follows; it is also the brief-source for the subagents it deploys.

> **Prime directive:** improve correctness, robustness, and real-world
> usability of the platform — while respecting the Phase 0 anti-feature
> list (`START_HERE.md` §3, `DEVELOPMENT.md`). Never pull Phase 1+ features
> forward without an explicit decision. Commit + push after every thrust so
> work is never lost.

---

## Roles

| Role | Agent | Mandate | May commit? |
|---|---|---|---|
| **Orchestrator** | the main session | Plan, triage findings, write specs, verify diffs, commit/push, decide convergence, surface checkpoints | Yes |
| **Implementer** | `general-purpose` | Build one specced change + its unit tests; run pytest+ruff locally; report | No (leaves diff for review) |
| **Reviewer/Auditor** | `general-purpose` (read-only mandate) | Find bugs/gaps/risks; report findings with severity; **do not fix** | No |
| **Validator** | `general-purpose` (background) | Run the suite + the relevant tier(s) on Alpine via sbatch; report pass/fail + numbers | No |

Subagents never see the orchestrator's conversation — every brief is
self-contained (repo path, branch, conventions, exact spec, done-criteria).

---

## The two nested loops

### Inner loop — a THRUST (one focused improvement)

```
PLAN ─▶ IMPLEMENT ─▶ VERIFY ─▶ VALIDATE ─▶ REVIEW(×N until converged) ─▶ RECORD
  │         │           │          │              │                        │
  orch   implementer   orch     validator     reviewer(s)                 orch
```

1. **PLAN** — orchestrator writes a precise spec (API signatures, files,
   tests, done-criteria) into the relevant plan doc and/or the implementer
   brief.
2. **IMPLEMENT** — one implementer subagent builds it + unit tests. (One
   agent, not parallel, when changes share files — parallel edits to the
   same files conflict.)
3. **VERIFY** — orchestrator reviews the actual diff (not just the agent's
   summary): runs `pytest` + `ruff`, reads the changed code, confirms it
   matches the spec and conventions. Then commits + pushes.
4. **VALIDATE** — validator subagent runs the change end-to-end on Alpine
   (background; queue waits are expected). Orchestrator records results.
5. **REVIEW (convergence rounds)** — a *fresh* reviewer subagent audits the
   thrust's code for correctness/robustness/security/usability findings.
   Orchestrator triages:
   - **Material finding** (correctness/robustness/security/data-loss, or a
     real usability trap) → spec a fix → mini IMPLEMENT+VERIFY → re-review.
   - **Nit / style / out-of-scope idea** → log it, don't act.
   Repeat until a review round returns **no material findings** = converged.
   Cap: 3 review rounds per thrust; if still finding material issues at 3,
   surface to the user (something structural is wrong).
6. **RECORD** — update `validation/RESULTS.md` / `docs/` as needed, commit.

### Outer loop — a CYCLE (audit-driven backlog)

```
FULL AUDIT (1–2 auditors, whole platform) ─▶ TRIAGE into prioritized thrusts
        ▲                                              │
        │                                              ▼
        └──────────── re-audit ◀── execute thrusts (each via inner loop)
```

1. **FULL AUDIT** — 1–2 auditor subagents review the *entire* codebase
   (not just the last thrust): correctness, concurrency, failure handling,
   API/doc consistency, test coverage, real-use gaps, scope discipline.
   Each returns a findings list with severities.
2. **TRIAGE** — orchestrator dedupes + ranks findings into thrusts
   (P0 correctness/data-loss first, then robustness, then usability, then
   nits). Drops anything that violates the anti-feature list.
3. **EXECUTE** — run each thrust through the inner loop, highest priority
   first.
4. **RE-AUDIT** — after the thrusts, run a fresh full audit.
5. **CYCLE CONVERGENCE** — a cycle is done when a full audit produces **no
   P0/P1 findings** and all aggregate validation gates (`PLAN.md §5`) pass.

---

## Convergence & stop conditions

- **Review round converges:** reviewer reports only nits / out-of-scope.
- **Thrust converges:** review converged AND its validation gates pass.
- **Cycle converges:** a full audit yields no P0/P1 findings AND aggregate
  gates green.
- **Global stop (surface to user):** TWO consecutive full audits yield zero
  P0/P1 findings → the platform is at a stable plateau; report and ask for
  the next thrust direction. Also stop on: a finding that needs a real
  product decision, a needed Phase-1 feature, repeated non-convergence
  (>3 rounds), or a destructive/irreversible action.

## Severity rubric (for reviewers/auditors)

- **P0** — data loss, double-execution, double-claim, corruption, security
  hole, or silent wrong results.
- **P1** — robustness gap that bites in normal operation (preemption,
  walltime, disk, dead worker, scale cliff), or API/doc mismatch that
  crashes a guided user.
- **P2** — usability/observability gap, missing-but-wanted ergonomics.
- **P3** — nit, style, doc polish, speculative idea.
Reviewers must assign a severity and a one-line justification per finding.

## Guardrails

- **Anti-feature discipline:** DAG, priors, artifact validation, GPU
  accounting, heartbeats, web UI, DB — do NOT build unless an audit shows a
  real, present need AND the user signs off. Auditors should flag scope
  creep as its own finding.
- **Engineering/infra decisions are autonomous** (sizing, queue/partition
  choice, compute routing) — don't stall on them.
- **Never** skip hooks, force-push, or take destructive cluster actions
  without explicit user approval.
- **Clean up** Alpine test pools after each validation run.
- **Trust but verify:** the orchestrator always reads the real diff before
  committing an implementer's work.

## When the orchestrator surfaces to the user

Only at: (a) cycle boundaries — a concise summary of what changed + current
health; (b) a genuine decision/Phase-1 trigger; (c) a global stop; (d) a
destructive action needing approval. Otherwise it self-drives via background
subagents (each completion notifies the orchestrator, which advances the
loop).

## Current state pointer

Live status of the loop (which thrust/cycle, open findings) is tracked in
the orchestrator's task list and summarized in `validation/RESULTS.md`.
`validation/PLAN.md` holds the tier/gate definitions; this file holds the
*process*.
