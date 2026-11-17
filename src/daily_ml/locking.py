"""Bounded POSIX advisory locks shared by the app and credential store."""

import errno
import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_POLL_SECONDS = 0.05


class LockBusy(RuntimeError):
    """A competing process is still holding a lock."""


@contextmanager
def file_lock(path: Path, *, wait: float) -> Iterator[None]:
    """Hold an exclusive lock on a persistent owner-only file.

    Parameters
    ----------
    path : Path
        Lock file inside an app-owned directory.
    wait : float
        Maximum time to wait in seconds. Zero fails immediately.

    Yields
    ------
    None
        While the process has exclusive access.

    Raises
    ------
    LockBusy
        If another owner holds the file at the deadline.
    """
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LockBusy("The local lock is busy.") from None
                time.sleep(min(remaining, LOCK_POLL_SECONDS))
        yield
    finally:
        os.close(descriptor)
