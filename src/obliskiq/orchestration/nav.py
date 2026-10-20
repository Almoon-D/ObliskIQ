"""The account's net asset value, read from the ledger and recorded as a series.

Ported from lite's ``collect/nav.py``: the figure is the ledger's consolidated ``BASE`` row, which
is already in the account's base currency (the ledger decoder refuses one in any other currency);
summing the per-currency rows at today's rates would drift with the rate rather than the book.
``store.nav.record_nav`` is the only writer of NAV rows, and this step its one caller. A NAV that
was not read is not a NAV of zero: the step fails with the reason and writes nothing.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ..config.schema import ObliskConfig
from ..execution.account import AccountReader
from ..store.nav import NavReading, record_nav
from ..store.worker import StoreWorker
from ..util.timeutil import utc_now

#: Where the reading comes from: the Client Portal Web API's ledger (docs/knowledge/ibkr.md, CP).
NAV_SOURCE = "cp"


@dataclass(frozen=True, slots=True)
class NavOutcome:
    """The reading recorded, or why there is none."""

    reading: NavReading | None
    reason: str


class NavCollector:
    """Reads one NAV from the ledger and records it under the day it was read."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        reader: AccountReader,
        worker: StoreWorker,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._base = settings.account.base_currency
        self._reader = reader
        self._worker = worker
        self._now = now

    async def collect(self, *, dry_run: bool = False) -> NavOutcome:
        cash = await self._reader.cash()
        if cash.nav is None:
            return NavOutcome(
                None,
                "the account ledger carried no base-currency net liquidation value"
                + (f" ({cash.nav_refusal})" if cash.nav_refusal else "")
                + "; a NAV that was not read is not a NAV of zero",
            )
        at = self._now()
        # as_of is the day the reading was TAKEN, never the day it is about.
        reading = NavReading(
            as_of=at.date(),
            nav_base=cash.nav,
            currency=self._base,
            source=NAV_SOURCE,
            recorded_at=at,
        )
        if not dry_run:
            await self._worker.run(record_nav, reading)
        return NavOutcome(reading, f"{cash.nav} {self._base}")
