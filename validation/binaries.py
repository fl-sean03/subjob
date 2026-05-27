"""Real-binary task generators (Cat B) — lab-agnostic.

Every generator produces a Task that invokes a real, non-Python or
heavy-Python binary. Each task writes a unique success marker to stdout
so the gate can confirm it actually ran (vs. silently crashing).

These are diverse to surface category-specific quirks: BLAS-backed
numerical compute (numpy/scipy), toolchain workloads (gcc compile),
Unix text-processing (awk pipeline), Python multiprocessing fork+IPC,
and heavy file I/O.

Modules-heavy HPC binaries (LAMMPS, GROMACS, QE, NAMD) are deferred
until the module-load chain is figured out — see PLAN.md § Remediation
> "Real binary fails to start". For each, a template lives in the
__future__ section and can be wired up once modules are resolved.
"""

from __future__ import annotations

from subjob.lib.task import Resources, Task

NUMPY_SUCCESS = "NUMPY_SVD_DONE"
SCIPY_SUCCESS = "SCIPY_ODE_DONE"
GCC_SUCCESS = "GCC_COMPILE_DONE"
AWK_SUCCESS = "AWK_PIPELINE_DONE"
MP_SUCCESS = "MP_FORK_DONE"
FILEIO_SUCCESS = "FILEIO_DONE"


def numpy_svd(task_id: str, n: int = 1000) -> Task:
    """Singular-value decomposition of an n×n matrix. Tests BLAS/numpy path."""
    cmd = (
        f"python3 -u -c \""
        f"import numpy as np, time\n"
        f"t = time.time()\n"
        f"A = np.random.RandomState(42).randn({n}, {n})\n"
        f"U, s, Vt = np.linalg.svd(A, full_matrices=False)\n"
        f"err = np.abs(A - U @ np.diag(s) @ Vt).max()\n"
        f"print(f'svd elapsed: {{time.time()-t:.2f}}s, n={n}, max_recon_err={{err:.3e}}')\n"
        f"print('{NUMPY_SUCCESS}')\""
    )
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=300))


def scipy_ode(task_id: str, n_steps: int = 100000) -> Task:
    """Integrate the Lorenz system. Tests scipy stack."""
    cmd = (
        f"python3 -u -c \""
        f"from scipy.integrate import solve_ivp\n"
        f"import numpy as np, time\n"
        f"def lorenz(t, y):\n"
        f"    x, y_, z = y\n"
        f"    return [10*(y_-x), x*(28-z)-y_, x*y_-2.667*z]\n"
        f"t = time.time()\n"
        f"sol = solve_ivp(lorenz, [0, 40], [1, 1, 1], max_step=1e-3, rtol=1e-6, atol=1e-9)\n"
        f"print(f'ode elapsed: {{time.time()-t:.2f}}s, points={{len(sol.t)}}, final={{sol.y[:,-1]}}')\n"
        f"print('{SCIPY_SUCCESS}')\""
    )
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=180))


def gcc_compile(task_id: str) -> Task:
    """gcc compile + run a tiny C program. Tests toolchain dispatch."""
    cmd = (
        "WORK=$(mktemp -d) && cd $WORK && "
        "cat > prog.c << 'EOF'\n"
        "#include <stdio.h>\n"
        "#include <math.h>\n"
        "int main() {\n"
        "    double s = 0.0;\n"
        "    for (long i = 1; i < 100000000L; i++) s += 1.0/i;\n"
        "    printf(\"harmonic sum=%.6f\\n\", s);\n"
        "    return 0;\n"
        "}\n"
        "EOF\n"
        f"gcc -O2 -lm -o prog prog.c && ./prog && echo {GCC_SUCCESS} && rm -rf $WORK"
    )
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=120))


def awk_pipeline(task_id: str, n_lines: int = 200000) -> Task:
    """Pipe-heavy text processing. Tests Unix toolchain + shell pipes."""
    cmd = (
        f"seq 1 {n_lines} | "
        "awk '{x = $1; sum += x; sumsq += x*x} END {n=NR; mean=sum/n; var=sumsq/n - mean*mean; "
        "printf \"n=%d mean=%.2f var=%.2f\\n\", n, mean, var}' | "
        "tee /dev/stderr | "
        f"grep -q 'mean=' && echo {AWK_SUCCESS}"
    )
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=60))


def multiprocessing_fork(task_id: str, n_procs: int = 4) -> Task:
    """Python multiprocessing.Pool — fork + IPC pattern."""
    cmd = (
        f"python3 -u -c \""
        f"import multiprocessing as mp, math, time\n"
        f"def work(x):\n"
        f"    return sum(math.sqrt(i) for i in range(x*1000))\n"
        f"t = time.time()\n"
        f"with mp.Pool({n_procs}) as p:\n"
        f"    rs = p.map(work, range(20))\n"
        f"print(f'mp elapsed: {{time.time()-t:.2f}}s, results={{len(rs)}}, sum={{sum(rs):.0f}}')\n"
        f"print('{MP_SUCCESS}')\""
    )
    return Task(
        id=task_id,
        command=cmd,
        resources=Resources(cores=n_procs, walltime_seconds=120),
    )


def heavy_file_io(task_id: str, size_mb: int = 200) -> Task:
    """Write then read a large file, checksum it. Tests sustained I/O capture."""
    cmd = (
        f"F=$(mktemp /tmp/subjob-fileio-{task_id}-XXX) && "
        f"dd if=/dev/urandom of=$F bs=1M count={size_mb} status=none && "
        f"SUM=$(sha256sum $F | cut -c1-12) && "
        f"echo wrote {size_mb}MB checksum=$SUM && "
        f"rm -f $F && echo {FILEIO_SUCCESS}"
    )
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=300))
