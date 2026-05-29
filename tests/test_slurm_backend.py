from __future__ import annotations

import subjob.backends.slurm as slurm_mod
from subjob.backends.slurm import SlurmBackend, _seconds_to_hms


def test_render_script_basic():
    backend = SlurmBackend(python_executable="/usr/bin/python3")
    script = backend.render_script(pool_dir="/scratch/pool", cores=64, walltime_seconds=86400)
    assert "#!/bin/bash" in script
    assert "#SBATCH --cpus-per-task=64" in script
    assert "#SBATCH --time=24:00:00" in script
    assert "/usr/bin/python3 -m subjob.worker --pool \"/scratch/pool\" --cores 64" in script
    # Worker must be exec'd so SLURM's SIGTERM reaches it directly (clean
    # preemption release). Without exec, bash is the signal target.
    assert "exec /usr/bin/python3 -m subjob.worker" in script


def test_render_script_with_idle_timeout():
    backend = SlurmBackend(python_executable="/usr/bin/python3")
    script = backend.render_script(
        pool_dir="/scratch/pool", cores=4, walltime_seconds=3600, idle_timeout_seconds=120
    )
    assert "--idle-timeout 120" in script


def test_render_script_no_idle_timeout_by_default():
    backend = SlurmBackend(python_executable="/usr/bin/python3")
    script = backend.render_script(pool_dir="/scratch/pool", cores=4, walltime_seconds=3600)
    assert "--idle-timeout" not in script


def test_render_script_with_partition_qos_gpu():
    backend = SlurmBackend(python_executable="/usr/bin/python3")
    script = backend.render_script(
        pool_dir="/scratch/p",
        cores=8,
        gpus=2,
        partition="aa100",
        qos="long",
        walltime_seconds=3600,
    )
    assert "#SBATCH --partition=aa100" in script
    assert "#SBATCH --qos=long" in script
    assert "#SBATCH --gres=gpu:2" in script
    assert "--gpus 2" in script


def test_seconds_to_hms():
    assert _seconds_to_hms(0) == "00:00:00"
    assert _seconds_to_hms(60) == "00:01:00"
    assert _seconds_to_hms(3661) == "01:01:01"
    assert _seconds_to_hms(7 * 86400) == "168:00:00"


def test_parse_sbatch_output():
    out = "Submitted batch job 12345678\n"
    assert SlurmBackend._parse_sbatch_output(out) == "12345678"


def test_parse_sbatch_output_malformed():
    import pytest

    with pytest.raises(RuntimeError):
        SlurmBackend._parse_sbatch_output("nothing useful")


def test_submit_worker_errors_when_sbatch_missing(tmp_path):
    backend = SlurmBackend(sbatch_cmd="definitely-not-sbatch-binary")
    import pytest

    with pytest.raises(RuntimeError, match="sbatch not found"):
        backend.submit_worker(pool_dir=str(tmp_path), cores=1)


def test_job_name_deterministic_and_pool_specific():
    a1 = SlurmBackend._job_name("/scratch/pool-a")
    a2 = SlurmBackend._job_name("/scratch/pool-a")
    b = SlurmBackend._job_name("/scratch/pool-b")
    assert a1 == a2  # deterministic for the same pool dir
    assert a1 != b  # differs by pool dir
    assert a1.startswith("subjob-")


def test_count_workers_returns_zero_when_squeue_absent(monkeypatch):
    backend = SlurmBackend()
    monkeypatch.setattr(slurm_mod.shutil, "which", lambda _cmd: None)
    assert backend.count_workers("/scratch/pool") == 0


def test_render_script_rejects_pool_dir_with_space():
    import pytest

    backend = SlurmBackend(python_executable="/usr/bin/python3")
    with pytest.raises(ValueError, match="must not contain whitespace or quotes"):
        backend.render_script(pool_dir="/scratch/my pool", cores=4, walltime_seconds=3600)


def test_render_script_rejects_qos_with_newline():
    """A qos containing a newline could inject arbitrary #SBATCH directives."""
    import pytest

    backend = SlurmBackend(python_executable="/usr/bin/python3")
    with pytest.raises(ValueError, match="must not contain a newline or quote"):
        backend.render_script(
            pool_dir="/scratch/pool",
            cores=4,
            walltime_seconds=3600,
            qos="long\n#SBATCH --account=victim",
        )


def test_render_script_uses_per_pool_job_name():
    backend = SlurmBackend(python_executable="/usr/bin/python3")
    script = backend.render_script(pool_dir="/scratch/pool", cores=4, walltime_seconds=3600)
    assert "#SBATCH --job-name=subjob-" in script
    assert "#SBATCH --job-name=subjob-worker\n" not in script
