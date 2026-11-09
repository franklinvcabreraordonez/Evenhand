"""A lock on a data folder, so that two proxies never write the same witness log.

The operating system releases it when the process ends, however it ends, so a crash never leaves a stale lock."""
from __future__ import annotations

import errno
import os
from typing import Optional


class FolderInUse(Exception):
    pass


class FolderLock:
    def __init__(self, folder: str):
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, ".lock")
        self._fh: Optional[object] = open(self.path, "a+b")
        try:
            self._acquire()
        except FolderInUse:
            self._fh.close()  # type: ignore[union-attr]
            self._fh = None
            raise

    def _acquire(self) -> None:
        fh = self._fh
        assert fh is not None
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)  # type: ignore[attr-defined]
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)):
                raise FolderInUse(f"another Evenhand process is already using {os.path.dirname(self.path)}")
            # Some other trouble with locking is not a reason to stop: carry on without it.

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()  # type: ignore[attr-defined]
            finally:
                self._fh = None