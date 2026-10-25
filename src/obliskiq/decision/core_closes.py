"""The month-end closes the core's trend tilt reads, from the account's stored bars (MASTER_PLAN
§5.4).

A vehicle's daily closes are the market store's (the collectors' backfill, ``collectors.backfill``),
in its listing currency. They are turned into one base-currency close per month end by
:func:`~obliskiq.engines.core.closes.month_end_closes`, the function the research panel's months
are read with; a vehicle in another currency is converted at the rate of each close's day from a
:class:`RateHistory`. The tilt is evaluated monthly: the month ends read run to the last completed
month, never the month in progress. A vehicle with a month lacking a fresh close, or a rate, gets
no closes, and the reason is kept (``MonthEnds.gaps``): the engine then names why it cannot read
the tilt.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol

from ..config.schema import ObliskConfig
from ..engines.core.closes import month_end_closes
from ..engines.core.view import MonthEndCloses, MonthEnds
from ..errors import TrendUnavailableError
from ..execution.instruments import InstrumentDirectory
from ..store import repos as market_repo
from ..store.worker import StoreWorker


class RateHistory(Protocol):
    """Reference rates over time, to convert a past close into the base currency."""

    async def load(self, as_of: date) -> None:
        """Read the rates public by ``as_of`` (off the event loop), once for a day."""
        ...

    def to_base(self, amount: Decimal, currency: str, on: date) -> Decimal | None:
        """``amount`` of ``currency`` in the base currency on ``on``; ``None`` without a fresh
        rate."""
        ...


def last_month_end(today: date) -> date:
    """The last completed month end before ``today``: the month the tilt is read at."""
    return today.replace(day=1) - timedelta(days=1)


class StoredCloses:
    """Month-end closes of the vehicles the core may hold, from the market store."""

    def __init__(
        self,
        settings: ObliskConfig,
        *,
        worker: StoreWorker,
        directory: InstrumentDirectory,
        rates: RateHistory | None,
    ) -> None:
        self._worker = worker
        self._directory = directory
        self._rates = rates
        self._base = settings.account.base_currency
        self._max_age = settings.research.simulation.max_price_age_days

    async def view(self, conids: Mapping[str, int], *, through: date) -> MonthEnds:
        """The closes of each vehicle (key to conid) through the month end ``through``."""
        if self._rates is not None:
            await self._rates.load(through)
        closes: dict[str, MonthEndCloses] = {}
        gaps: dict[str, str] = {}
        for key, conid in sorted(conids.items()):
            instrument = self._directory.describe(conid)
            if instrument is None:
                gaps[key] = f"{conid} is not described, so its currency is unknown"
                continue
            bars = await self._worker.run(market_repo.bar_series, conid, as_of=through)
            currency = instrument.currency

            def convert(day: date, value: Decimal, currency: str = currency) -> Decimal | None:
                if currency == self._base:
                    return value
                return None if self._rates is None else self._rates.to_base(value, currency, day)

            try:
                closes[key] = month_end_closes(
                    [(bar.session_date, bar.close) for bar in bars],
                    convert,
                    through=through,
                    max_age_days=self._max_age,
                    name=f"{instrument.symbol} ({conid})",
                )
            except TrendUnavailableError as exc:
                gaps[key] = str(exc)
                continue
            if not closes[key]:
                del closes[key]
                gaps[key] = f"{instrument.symbol} ({conid}) has no stored daily closes"
        return MonthEnds(through, closes, gaps)
