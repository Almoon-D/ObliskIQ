"""The collectors over the fake market: a started read client, a store worker and a clock.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import respx
from fake_broker import ACCOUNT
from fake_market import FakeMarket

from obliskiq.calendars.service import Calendars
from obliskiq.config import ObliskConfig
from obliskiq.data.market.daily import MarketCollector
from obliskiq.ibkr.client import IbkrClient
from obliskiq.ibkr.guard import AccountGuard
from obliskiq.ibkr.session import BrokerSession
from obliskiq.store.db import open_database
from obliskiq.store.worker import StoreWorker

#: After the European and New York closes of Wednesday 2026-10-21.
AFTER_CLOSE = datetime(2026, 10, 21, 21, 0, tzinfo=UTC)
#: Shipped universe instruments the tests collect.
EMIM, IBGM, ASML, SPY, EWJ, SPX = 153454120, 43614383, 117589399, 756733, 253190534, 416904


class Now:
    """A settable wall clock."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@dataclass
class MarketRig:
    settings: ObliskConfig
    market: FakeMarket
    client: IbkrClient
    worker: StoreWorker
    collector: MarketCollector
    now: Now
    session: BrokerSession

    async def store(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        return await self.worker.run(function, *args, **kwargs)

    async def aclose(self) -> None:
        await self.worker.aclose()
        await self.session.aclose()


async def build_market_rig(
    *,
    settings: ObliskConfig,
    session: BrokerSession,
    router: respx.MockRouter,
    calendars: Calendars,
    tmp_path: Path,
) -> MarketRig:
    market = FakeMarket(router)
    guard = AccountGuard(account_id=ACCOUNT, profile="paper", paper_prefixes=("DU",))
    client = IbkrClient(session, settings, guard)
    await client.start()
    worker = StoreWorker(
        lambda: open_database(
            tmp_path / "market.db",
            busy_timeout_ms=settings.store.busy_timeout_ms,
            synchronous=settings.store.synchronous,
        )
    )
    now = Now(AFTER_CLOSE)
    collector = MarketCollector(
        settings,
        client=client,
        worker=worker,
        calendars=calendars,
        now=now,
        sleep=session.clock.sleep,
    )
    return MarketRig(settings, market, client, worker, collector, now, session)
