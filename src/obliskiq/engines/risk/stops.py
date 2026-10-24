"""Volatility-scaled stops and the trailing stop (the equity side of lite's risk engine).

**A stop is volatility-scaled, always.** ``equity_risk.stop_type`` is ``volatility_scaled`` and
there is no branch that produces a fixed percentage: a 20 % stop is noise on a 90-vol name and far
too loose on a 15-vol one. The distance is ``equity_risk.stop_atr_multiple`` average true ranges
below the entry, the ATR the simple mean of ``equity_risk.atr_period`` true ranges (gaps included:
a name that opens six percent lower has a true range far wider than its intraday span).

**The trailing stop protects a gain without capping it**: ``trailing_stop_atr_multiple`` ATRs
below the running high, armed only when ``trailing_stop_enabled``; the effective stop is the
higher of the two. Short history gives no stop, never a guessed one. The protective spec's limit
below the stop is the execution layer's (``execution/protective.py``, DECISIONS D67).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass

from ...config.schema import ObliskConfig
from ...domain.market import Bar
from ...quant.stats import average_true_range


@dataclass(frozen=True, slots=True)
class StopLevels:
    """A position's volatility-scaled stops."""

    atr: float
    atr_period: int
    entry_price: float
    stop: float
    stop_distance_pct: float
    trailing_high: float | None
    trailing_stop: float | None
    stop_atr_multiple: float
    trailing_atr_multiple: float

    @property
    def effective_stop(self) -> float:
        """The higher of the two once the position has run in its favour."""
        if self.trailing_stop is None:
            return self.stop
        return max(self.stop, self.trailing_stop)


class StopEngine:
    """Stops from the bars, bound to configuration once."""

    def __init__(self, settings: ObliskConfig) -> None:
        risk = settings.equity_risk
        self._stop_type = risk.stop_type
        self._stop_multiple = risk.stop_atr_multiple
        self._trailing_enabled = risk.trailing_stop_enabled
        self._trailing_multiple = risk.trailing_stop_atr_multiple
        self._period = risk.atr_period

    @property
    def stop_type(self) -> str:
        return self._stop_type

    def atr(self, bars: Sequence[Bar]) -> float | None:
        """The simple mean of the last ``equity_risk.atr_period`` true ranges; ``None`` on short
        history (a bar without a high or a low is skipped, never filled)."""
        return average_true_range(
            [bar.high for bar in bars],
            [bar.low for bar in bars],
            [bar.close for bar in bars],
            period=self._period,
        )

    def stops(
        self, bars: Sequence[Bar], *, entry_price: float, trailing_high: float | None = None
    ) -> StopLevels | None:
        """The stop and the trailing stop; ``None`` when the bars hold too few true ranges or
        the entry is not a price."""
        atr = self.atr(bars)
        if atr is None or atr <= 0 or entry_price <= 0:
            return None
        stop = entry_price - self._stop_multiple * atr
        trailing = None
        if self._trailing_enabled and trailing_high is not None:
            trailing = trailing_high - self._trailing_multiple * atr
        return StopLevels(
            atr=atr,
            atr_period=self._period,
            entry_price=entry_price,
            stop=stop,
            stop_distance_pct=(entry_price - stop) / entry_price,
            trailing_high=trailing_high,
            trailing_stop=trailing,
            stop_atr_multiple=self._stop_multiple,
            trailing_atr_multiple=self._trailing_multiple,
        )
