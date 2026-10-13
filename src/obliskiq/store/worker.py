"""One store on one thread, for async callers.

A :class:`~obliskiq.store.db.Store` belongs to the thread that opened it, and a write may wait
for another process's lock, so the event loop never calls it directly: the worker opens the store
on its own single thread and runs each call there.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Concatenate

from .db import Store


class StoreWorker:
    """Runs store functions on one dedicated thread; the store is opened there on first use."""

    def __init__(self, open_store: Callable[[], Store]) -> None:
        self._open = open_store
        self._store: Store | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="obliskiq-store")

    def _call[**P, T](
        self, function: Callable[Concatenate[Store, P], T], *args: P.args, **kwargs: P.kwargs
    ) -> T:
        if self._store is None:
            self._store = self._open()
        return function(self._store, *args, **kwargs)

    async def run[**P, T](
        self, function: Callable[Concatenate[Store, P], T], *args: P.args, **kwargs: P.kwargs
    ) -> T:
        """``function(store, *args, **kwargs)`` on the worker's thread."""
        loop = asyncio.get_running_loop()
        call = functools.partial(self._call, function, *args, **kwargs)
        return await loop.run_in_executor(self._executor, call)

    def _close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    async def aclose(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._close)
        self._executor.shutdown(wait=True)
