"""The store worker: one store, opened and used on one thread, for async callers.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import threading
from pathlib import Path

from obliskiq.store import Store, open_database
from obliskiq.store.worker import StoreWorker


async def test_every_call_runs_on_the_one_thread_that_opened_the_store(tmp_path: Path) -> None:
    opened_on: list[int] = []

    def open_one() -> Store:
        opened_on.append(threading.get_ident())
        return open_database(tmp_path / "w.db", busy_timeout_ms=1000, synchronous="NORMAL")

    worker = StoreWorker(open_one)

    def where(store: Store, extra: int, *, more: int) -> tuple[int, int]:
        store.execute("SELECT 1")
        return threading.get_ident(), extra + more

    first = await worker.run(where, 1, more=2)
    second = await worker.run(where, 3, more=4)
    assert first[0] == second[0] == opened_on[0] != threading.get_ident()
    assert (first[1], second[1]) == (3, 7)
    assert len(opened_on) == 1
    await worker.aclose()


async def test_a_worker_never_used_closes_cleanly(tmp_path: Path) -> None:
    worker = StoreWorker(
        lambda: open_database(tmp_path / "x.db", busy_timeout_ms=1, synchronous="NORMAL")
    )
    await worker.aclose()
