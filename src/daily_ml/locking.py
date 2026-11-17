"""Bounded POSIX advisory locks shared by the app and credential store."""

import errno
import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event

LOCK_POLL_SECONDS = 0.05


class LockBusy(RuntimeError):
    """A competing process is still holding a lock."""


@contextmanager
def file_lock(path: Path, *, wait: float, cancelled: Event | None = None) -> Iterator[None]:
    """Hold an exclusive lock on a persistent owner-only file.

    Parameters
    ----------
    path : Path
        Lock file inside an app-owned directory.
    wait : float
        Maximum time to wait in seconds. Zero fails immediately.
    cancelled : Event or None
        Optional signal that interrupts a pending acquisition.

    Yields
    ------
    None
        While the process has exclusive access.

    Raises
    ------
    LockBusy
        If another owner holds the file at the deadline or acquisition was cancelled.
    """
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + wait
        while True:
            if cancelled is not None and cancelled.is_set():
                raise LockBusy("The local lock wait was cancelled.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LockBusy("The local lock is busy.") from None
                interval = min(remaining, LOCK_POLL_SECONDS)
                if cancelled is None:
                    time.sleep(interval)
                else:
                    cancelled.wait(interval)
        yield
    finally:
        os.close(descriptor)
