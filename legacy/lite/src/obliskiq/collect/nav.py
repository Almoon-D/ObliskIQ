"""The account's net asset value, read from the ledger and recorded as a series.

**Nothing wrote ``nav_history``.** The table has existed since migration 003 and
its only writer was a method on the risk engine that nothing in ``src/`` called,
so on a store the collectors filled alone every NAV reader had the same answer:
none. That is not a cosmetic gap. The sleeve budget is a fraction of NAV, the
affordability ceiling is derived from the budget, and the census refuses outright
without one — correctly, since the budget defines the universe — which meant the
weekly ``compute@census`` timer was scheduled to report the same refusal forever.
The rolling-month drawdown breaker was in the same position, reporting ``n/a``
rather than measuring anything.

The figure comes from ``/portfolio/{account}/ledger``, whose consolidated
``BASE`` row is already in the account's base currency. Summing the per-currency
rows at today's FX would produce a number that drifts with the rate rather than
with the book, which is the opposite of what a drawdown series is for.

**Two cadences, deliberately.** ``obliskiq-collect nav`` runs inside the weekday
sequence and the weekly ``universe`` sync records one too, through this same
class. The census reads the latest observation and wants it fresh; the breaker
reads a 30-day window and wants enough points in it to mean something. One
writer, two callers — a second implementation would eventually disagree with this
one about which currency it was in.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from __future__ import annotations

from ..config import Config
from ..logging_setup import get_logger
from ..store import Store
from ..store.nav import record_nav
from ..util.timeutil import utc_today
from .retry import TargetRetryPolicy, with_target_retry
from .runlog import CollectorRun
from .source import MarketData

log = get_logger("collect.nav")

COLLECTOR_NAME = "nav"

#: The run-log target name. Not a symbol: it is one reading about the whole
#: account, and naming it here keeps the run log and its readers from disagreeing.
NAV_TARGET = "nav"


class NavCollector:
    """Reads one NAV from the account ledger and appends it to ``nav_history``."""

    def __init__(self, store: Store, config: Config, market_data: MarketData) -> None:
        self._store = store
        self._config = config
        self._market_data = market_data
        self._policy = TargetRetryPolicy.from_config(config)
        self._base_currency = config.require_str("account.base_currency")

    async def run(self, run: CollectorRun, *, dry_run: bool = False) -> int:
        """Record today's observation. Returns the number of rows written."""
        try:
            observation = await with_target_retry(
                self._policy, NAV_TARGET, self._market_data.account_nav
            )
        except Exception as exc:
            run.failed(NAV_TARGET, f"could not read the account NAV: {exc!r}")
            return 0

        if observation is None:
            run.failed(
                NAV_TARGET,
                "the account ledger carried no base-currency net liquidation value. "
                "A NAV that was not read is not a NAV of zero, and every gate "
                "downstream is a fraction of this number.",
            )
            return 0

        if observation.currency is not None and observation.currency != self._base_currency:
            # Every gate reading NAV compares it against a premium already
            # converted to base, so a figure in another currency does not fail —
            # it mis-sizes the sleeve by the exchange rate, silently. Refused for
            # the same reason option_ledger refuses an amount it cannot convert.
            run.failed(
                NAV_TARGET,
                f"the account ledger reports its net liquidation value in "
                f"{observation.currency}, and account.base_currency is "
                f"{self._base_currency}. A NAV in the wrong currency mis-sizes every "
                "gate that reads it, by an exchange rate nobody would see.",
            )
            return 0

        if not dry_run:
            # as_of is the day the reading was TAKEN, never the day it is about.
            # The breaker reads this table as a series, so back-dating a figure
            # read today onto an earlier session puts a number the account never
            # had into the window it measures.
            record_nav(
                self._store,
                as_of=utc_today(),
                nav=observation.nav_base,
                source=self._market_data.source,
            )
        run.succeeded(NAV_TARGET, 1, currency=observation.currency)
        return 1
