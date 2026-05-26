"""subjob — pilot-job scheduler for HPC.

See docs/ARCHITECTURE.md for the design.
"""

from subjob.lib.pool import Pool
from subjob.lib.task import Task

__all__ = ["Pool", "Task"]
__version__ = "0.1.0"
