"""Backends submit the worker process to a scheduler.

A backend's job is narrow: take a pool dir + resource ask, return a handle
to a running worker. The worker itself is backend-agnostic.
"""

from subjob.backends.base import Backend, WorkerHandle, WorkerStatus
from subjob.backends.local import LocalBackend
from subjob.backends.slurm import SlurmBackend

__all__ = [
    "Backend",
    "WorkerHandle",
    "WorkerStatus",
    "LocalBackend",
    "SlurmBackend",
    "make_backend",
]


def make_backend(name: str, **kwargs) -> Backend:
    """Construct a backend by name. Supports "slurm" and "local"."""
    if name == "slurm":
        return SlurmBackend(**kwargs)
    if name == "local":
        return LocalBackend(**kwargs)
    raise ValueError(f"unknown backend: {name!r} (expected 'slurm' or 'local')")
