"""Atomic claim primitive.

The entire concurrency story of subjob is `os.rename` between two
directories on the same POSIX filesystem. No lockfiles, no flock — those
are flaky on the shared filesystems we target.

Invariant: at most one process succeeds at moving `pending/<id>.yaml` →
`claimed/<id>.yaml`. Losers see FileNotFoundError (the file already moved)
and treat it as a normal race loss.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path


class CrossFilesystemError(RuntimeError):
    """Pool dirs must live on a single filesystem (rename must be atomic)."""


def atomic_move(src: Path, dst: Path) -> bool:
    """Atomically move src → dst. Returns True on success, False if we lost the race.

    Raises CrossFilesystemError if src and dst are on different filesystems
    (rename is not atomic in that case — see ARCHITECTURE.md).
    """
    try:
        os.rename(src, dst)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise CrossFilesystemError(
                f"src ({src}) and dst ({dst}) are on different filesystems; "
                "the pool must live on a single shared filesystem"
            ) from e
        raise
