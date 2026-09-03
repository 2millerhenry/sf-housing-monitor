"""Cross-platform, non-blocking exclusive file lock.

One lock file keeps scheduled, manual, command-line, and service scans from
overlapping across *processes*. POSIX uses ``fcntl.flock``; Windows has no
``fcntl`` at all, so it uses ``msvcrt.locking`` over a one-byte region. Both
are advisory, both are released when the handle closes, and both are released
by the operating system if the owning process dies -- which is what the scanner
depends on to recover after a crash mid-scan.
"""

from __future__ import annotations

import errno
from typing import IO

try:  # POSIX
    import fcntl

    msvcrt = None
except ModuleNotFoundError:  # Windows
    fcntl = None
    import msvcrt


# Windows signals "another process holds this region" with these errno values.
# EDEADLOCK is 36 there and simply absent on macOS, so it cannot be referenced
# directly at import time. Anything outside this set is a genuine failure and
# must keep propagating rather than being misread as a busy lock.
_WINDOWS_CONTENTION = frozenset({errno.EACCES, getattr(errno, "EDEADLOCK", 36)})


def try_acquire(handle: IO) -> bool:
    """Take an exclusive lock without blocking.

    Returns ``False`` when another process already holds the lock. Any other
    error is raised, so a broken lock file never silently reads as "a scan is
    already running".
    """
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    # msvcrt locks a byte range starting at the current position, and the
    # scanner opens the lock file in append mode, so the position must be
    # pinned to 0 for the lock and the unlock to name the same region.
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno in _WINDOWS_CONTENTION:
            return False
        raise
    return True


def release(handle: IO) -> None:
    """Release a lock taken by :func:`try_acquire`."""
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        # The caller closes the handle immediately after this, which releases
        # the region anyway. An already-released region must not turn a
        # completed scan into a crash.
        pass
