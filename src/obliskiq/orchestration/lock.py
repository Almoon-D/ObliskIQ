"""One process runs the executor (DECISIONS D47): the service lock.

``obliskiq serve`` and every order command take an exclusive ``flock`` on ``paths.service_lock``
in the data directory for as long as they run. The kernel releases it when the process ends, however
it ends, so a lock left by a killed process never blocks the next start (a PID file would, and a
PID is reused). Holding it is also what lets the next start close a dead process's run records at
once (``store.runs.reap_running``): nothing else can be writing them.

Linux and macOS only (``fcntl``), like the Gateway the service runs beside.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import contextlib
import fcntl
import json
import os
from pathlib import Path
from typing import IO, Self

from ..errors import ObliskError
from ..util.timeutil import utc_stamp


class ServiceLockedError(ObliskError):
    """Another obliskiq process holds the service lock."""


class ServiceLock:
    """An exclusive lock on a file, held until :meth:`release` or the process ends."""

    def __init__(self, path: Path, *, command: str) -> None:
        self._path = path
        self._command = command
        self._handle: IO[str] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> Self:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            holder = handle.read().strip() or "another obliskiq process"
            handle.close()
            raise ServiceLockedError(
                f"another obliskiq process runs the executor ({holder}); stop it first"
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps({"command": self._command, "pid": os.getpid(), "since": utc_stamp()})
        )
        handle.flush()
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            handle.seek(0)
            handle.truncate()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()
