"""Backends submit the worker process to a scheduler.

A backend's job is narrow: take a pool dir + resource ask, return a handle
to a running worker. The worker itself is backend-agnostic.
"""

from subjob.backends.base import Backend, WorkerHandle, WorkerStatus

__all__ = ["Backend", "WorkerHandle", "WorkerStatus"]
