"""Synthetic, lab-agnostic task generators.

Every workload here is shell + standard tools (bash, dd, yes, python3) so
the suite runs on any cluster without external deps. The point is to
exercise subjob's runner under varied command shapes, not to model science.

`python3` in task commands resolves via `_PY` to the same interpreter that
imports this module — so task tasks see the same site-packages the
submitter has (matters on HPC where system python3 may be Python 3.6).
"""

from __future__ import annotations

import random
import sys

from subjob.lib.task import Resources, Task

# Use the submitter's interpreter so children see the same site-packages
# (e.g., scipy installed via pip --user in Python 3.10 isn't visible to
# Alpine's system /usr/bin/python3 = Python 3.6).
_PY = sys.executable


def cpu_spin(task_id: str, seconds: float = 5.0, walltime_seconds: int | None = None) -> Task:
    """CPU-bound task. Bursts a tight Python loop for `seconds`."""
    wt = walltime_seconds or max(int(seconds * 3) + 5, 10)
    cmd = (
        f"{_PY} -c \"import time, math\n"
        f"end = time.time() + {seconds}\n"
        "n = 0.0\n"
        "while time.time() < end:\n"
        "    n += sum(math.sqrt(i) for i in range(1000))\n"
        "print('cpu_spin done', round(time.time(), 3))\""
    )
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=wt))


def mem_alloc(task_id: str, gb: float = 1.0, hold_s: float = 1.0) -> Task:
    """Allocate `gb` GB of bytes, hold for `hold_s`, release."""
    nbytes = int(gb * (1 << 30))
    cmd = (
        f"{_PY} -c \""
        f"import time; x = b'x'*{nbytes}; "
        f"print('mem_alloc holding', {gb}, 'GB'); "
        f"time.sleep({hold_s}); "
        "del x; print('mem_alloc done')\""
    )
    return Task(
        id=task_id,
        command=cmd,
        resources=Resources(cores=1, memory_gb=max(int(gb * 2), 1), walltime_seconds=120),
    )


def disk_io(task_id: str, mb: int = 100, dir_path: str = "/tmp") -> Task:
    """Write `mb` MB to a scratch file, read it back, delete."""
    f = f"{dir_path}/subjob-disk-io-{task_id}-$$"
    cmd = (
        f"dd if=/dev/zero of={f} bs=1M count={mb} status=none && "
        f"dd if={f} of=/dev/null bs=1M status=none && "
        f"rm -f {f} && echo disk_io done {mb} MB"
    )
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=120))


def stdout_firehose(task_id: str, lines: int = 10000) -> Task:
    """Emit `lines` of output. Tests log capture + 4 KB tail truncation."""
    cmd = f"yes 'subjob stdout firehose line' | head -n {lines}"
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=60))


def signal_grace(task_id: str) -> Task:
    """Catches SIGTERM via bash trap, prints, exits. Walltime fires SIGTERM.

    Uses bash trap instead of Python signal handlers — avoids shell-escape
    gymnastics with multi-line Python source inside subprocess shell=True.
    """
    cmd = (
        "trap 'echo caught SIGTERM, exiting cleanly; exit 0' TERM; "
        "echo signal_grace running; "
        "sleep 60 & wait"
    )
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=2))


def signal_ignore(task_id: str) -> Task:
    """Ignores SIGTERM. Tests SIGKILL fallback in the runner."""
    cmd = (
        "trap '' TERM; "
        "echo signal_ignore running, will ignore SIGTERM; "
        "sleep 60"
    )
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=2))


def exit_code(task_id: str, rc: int) -> Task:
    """Exit with a specific code (0-255)."""
    cmd = f"echo exit_code task returning {rc} && exit {rc}"
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=10))


def segfault(task_id: str) -> Task:
    """Deliberate segfault. Tests signal-death exit code propagation."""
    cmd = f"{_PY} -c \"import ctypes; ctypes.string_at(0)\""
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=10))


def sleep_random(task_id: str, lo: float = 1.0, hi: float = 5.0, seed: int | None = None) -> Task:
    """Variable-duration sleep. Used for scheduling tests with mixed durations."""
    rng = random.Random(seed) if seed is not None else random
    s = rng.uniform(lo, hi)
    cmd = f"sleep {s:.2f} && echo sleep_random {s:.2f}s done"
    return Task(
        id=task_id,
        command=cmd,
        resources=Resources(cores=1, walltime_seconds=int(hi) + 5),
    )


def echo_only(task_id: str, message: str = "hello") -> Task:
    """Trivial task — instant completion. Throughput baseline."""
    cmd = f"echo {message}"
    return Task(id=task_id, command=cmd, resources=Resources(cores=1, walltime_seconds=10))
