"""subjob — pilot-job scheduler for HPC.

See docs/ARCHITECTURE.md for the design.

Public API (re-exported once the modules exist):
    from subjob import Pool, Task
"""

__version__ = "0.1.0"


def __getattr__(name):
    if name == "Pool":
        from subjob.lib.pool import Pool

        return Pool
    if name == "Task":
        from subjob.lib.task import Task

        return Task
    raise AttributeError(name)
