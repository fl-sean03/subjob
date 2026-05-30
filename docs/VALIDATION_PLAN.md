# Validation plan — pointer

The full, operational validation & benchmarking plan lives alongside the harness:

  → **[`validation/PLAN.md`](../validation/PLAN.md)**

Brief overview:

- **Goal:** lab-agnostic, ground-up validation of subjob across the full HPC software stack (LAMMPS, GROMACS, Quantum ESPRESSO, NAMD, PyTorch, generic Python) before any project-specific dogfood (MXene, hydrogenation).
- **Approach:** 8 tiers, gate-driven, iterative scaling. Each tier must pass its gates before the next runs.
- **Harness:** Stdlib-only Python under `validation/`. Generators for synthetic workloads (`workloads.py`), gate predicates (`gates.py`), tier runner (`runner.py`), markdown reports (`report.py`), per-tier specs under `tiers/`.
- **Pass criterion for "Phase 0 fully validated":** all 9 aggregate gates in `validation/PLAN.md § 5`.
- **Remediation playbook** for every expected failure class: `validation/PLAN.md § 6`.
