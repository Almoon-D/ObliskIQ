"""Where clearances are spent: the store, once, for exactly the write they were issued for.

The clearance presented must be the one the gate recorded, field for field (a copy with an
altered effect, quote or expiry is refused), and the store's single ``UPDATE`` spends it only for
the same intent, action, broker order and terms digest, before it expires and if it is unspent.
Two processes cannot both spend one clearance.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from datetime import datetime

from ..domain.orders import ClearanceAction, OrderTerms, PretradeClearance
from ..errors import WriteRefusedError
from ..store import orders as repo
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now


class StoreClearanceLedger:
    """The clearance ledger the order client spends from."""

    def __init__(self, worker: StoreWorker, *, now: Callable[[], datetime] = utc_now) -> None:
        self._worker = worker
        self._now = now

    async def spend(
        self,
        clearance: PretradeClearance,
        *,
        action: ClearanceAction,
        order_id: str | None,
        terms: OrderTerms,
    ) -> None:
        stored = await self._worker.run(repo.get_clearance, clearance.clearance_id)
        if stored is None:
            raise WriteRefusedError("clearance_unknown", "the gate issued no such clearance")
        if stored.clearance != clearance:
            raise WriteRefusedError(
                "clearance_altered", "the clearance presented is not the one the gate recorded"
            )
        refusal = await self._worker.run(
            repo.spend_clearance,
            clearance.clearance_id,
            intent_id=clearance.intent_id,
            action=action,
            order_id=order_id,
            terms_digest=terms.digest(),
            now=self._now(),
        )
        if refusal is not None:
            raise WriteRefusedError("clearance_refused", refusal)
