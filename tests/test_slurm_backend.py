from __future__ import annotations

from subjob.backends.slurm import SlurmBackend, _seconds_to_hms


def test_render_script_basic():
    backend = SlurmBackend(python_executable="/usr/bin/python3")
    script = backend.render_script(pool_dir="/scratch/pool", cores=64, walltime_seconds=86400)
    assert "#!/bin/bash" in script
    assert "#SBATCH --cpus-per-task=64" in script
    assert "#SBATCH --time=24:00:00" in script
    assert "/usr/bin/python3 -m subjob.worker --pool \"/scratch/pool\" --cores 64" in script


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
