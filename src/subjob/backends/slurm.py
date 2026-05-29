"""SLURM backend — submit a subjob worker as an sbatch job.

Minimal Phase 0 implementation: generate an sbatch script that runs
`python -m subjob.worker --pool <dir>` and submit it via the `sbatch` CLI.
Returns the SLURM job id.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from subjob.backends.base import WorkerHandle, WorkerStatus

SBATCH_TEMPLATE = """\
#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={cores}
#SBATCH --time={time_str}
#SBATCH --output={log_dir}/worker-%j.log
{extra_directives}

set -euo pipefail

# Worker uses SLURM_JOB_START_TIME + SLURM_JOB_TIMELIMIT for walltime awareness.
# `exec` REPLACES this shell with the worker so SLURM's SIGTERM (sent on
# scancel / walltime end / preemption) is delivered directly to the worker
# process — letting it catch the signal, kill in-flight tasks, and release
# its claims cleanly. Without exec, bash is the signal target and the worker
# is only reaped by the later SIGKILL, orphaning its claims.
exec {python} -m subjob.worker --pool "{pool_dir}" --cores {cores} {gpu_flag} {idle_flag}
"""


class SlurmBackend:
    name = "slurm"

    def __init__(self, sbatch_cmd: str = "sbatch", python_executable: str | None = None):
        self.sbatch_cmd = sbatch_cmd
        self.python_executable = python_executable or sys.executable

    def submit_worker(
        self,
        *,
        pool_dir: str,
        cores: int,
        gpus: int = 0,
        walltime_seconds: int = 86400,
        partition: str | None = None,
        qos: str | None = None,
        idle_timeout_seconds: float | None = None,
        extra_sbatch_args: list[str] | None = None,
    ) -> WorkerHandle:
        _validate_pool_dir(pool_dir)
        _validate_directive_value("partition", partition)
        _validate_directive_value("qos", qos)
        for arg in extra_sbatch_args or []:
            _validate_directive_value("extra_sbatch_args item", arg)
        if shutil.which(self.sbatch_cmd) is None:
            raise RuntimeError(f"sbatch not found on PATH (looked for {self.sbatch_cmd!r})")

        script = self.render_script(
            pool_dir=pool_dir,
            cores=cores,
            gpus=gpus,
            walltime_seconds=walltime_seconds,
            partition=partition,
            qos=qos,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        pool_path = Path(pool_dir)
        (pool_path / "logs").mkdir(parents=True, exist_ok=True)
        script_path = pool_path / "logs" / f"worker-{os.getpid()}.sbatch"
        script_path.write_text(script)

        cmd = [self.sbatch_cmd]
        if extra_sbatch_args:
            cmd.extend(extra_sbatch_args)
        cmd.append(str(script_path))
        out = subprocess.check_output(cmd, text=True)
        job_id = self._parse_sbatch_output(out)
        return WorkerHandle(
            backend=self.name,
            job_id=job_id,
            metadata={"script": str(script_path), "pool_dir": pool_dir},
        )

    def render_script(
        self,
        *,
        pool_dir: str,
        cores: int,
        gpus: int = 0,
        walltime_seconds: int = 86400,
        partition: str | None = None,
        qos: str | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> str:
        # `#SBATCH --output=` and the worker `--pool` arg interpolate pool_dir.
        # SLURM directives can't be reliably quoted, so a pool_dir with
        # whitespace/quotes would silently truncate the log path — reject it.
        _validate_pool_dir(pool_dir)
        # partition/qos are interpolated raw into #SBATCH directives; a newline
        # or quote would inject arbitrary directives. Reject them like pool_dir.
        _validate_directive_value("partition", partition)
        _validate_directive_value("qos", qos)
        extras = []
        if partition:
            extras.append(f"#SBATCH --partition={partition}")
        if qos:
            extras.append(f"#SBATCH --qos={qos}")
        if gpus > 0:
            extras.append(f"#SBATCH --gres=gpu:{gpus}")
        return SBATCH_TEMPLATE.format(
            job_name=self._job_name(pool_dir),
            cores=cores,
            time_str=_seconds_to_hms(walltime_seconds),
            log_dir=f"{pool_dir}/logs",
            extra_directives="\n".join(extras),
            python=self.python_executable,
            pool_dir=pool_dir,
            gpu_flag=f"--gpus {gpus}" if gpus > 0 else "",
            idle_flag=f"--idle-timeout {idle_timeout_seconds}" if idle_timeout_seconds else "",
        )

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        if shutil.which("squeue") is None:
            return WorkerStatus(state="unknown", detail="squeue not available")
        out = subprocess.run(
            ["squeue", "-h", "-j", handle.job_id, "-o", "%T"],
            capture_output=True,
            text=True,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return WorkerStatus(state="completed", detail="job not in queue")
        return WorkerStatus(state=out.stdout.strip().lower())

    def cancel(self, handle: WorkerHandle) -> None:
        if shutil.which("scancel") is None:
            raise RuntimeError("scancel not available")
        subprocess.run(["scancel", handle.job_id], check=True)

    def count_workers(self, pool_dir: str) -> int:
        """Count this pool's worker jobs in the queue (best-effort).

        Matches by the per-pool job name so distinct pools don't collide.
        Returns 0 when squeue is unavailable (e.g. off-cluster).
        """
        if shutil.which("squeue") is None:
            return 0
        user = os.environ.get("USER", "")
        out = subprocess.run(
            ["squeue", "-h", "-u", user, "-n", self._job_name(pool_dir), "-o", "%i"],
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            return 0
        return sum(1 for line in out.stdout.splitlines() if line.strip())

    @staticmethod
    def _job_name(pool_dir: str) -> str:
        return "subjob-" + hashlib.sha1(pool_dir.encode()).hexdigest()[:8]

    @staticmethod
    def _parse_sbatch_output(out: str) -> str:
        m = re.search(r"Submitted batch job (\d+)", out)
        if not m:
            raise RuntimeError(f"could not parse sbatch output: {out!r}")
        return m.group(1)


def _validate_pool_dir(pool_dir: str) -> None:
    """Reject pool_dirs that can't be safely interpolated into #SBATCH directives."""
    if any(c.isspace() for c in pool_dir) or '"' in pool_dir:
        raise ValueError(
            f"pool_dir must not contain whitespace or quotes for the SLURM backend: {pool_dir!r}"
        )


def _validate_directive_value(label: str, value: str | None) -> None:
    """Reject sbatch values that could inject extra directives via newline/quote."""
    if value is None:
        return
    if "\n" in value or '"' in value:
        raise ValueError(f"{label} must not contain a newline or quote: {value!r}")


def _seconds_to_hms(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
