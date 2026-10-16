"""The quote policy per asset class and the collar (MASTER_PLAN §5.12, §5.13).

Defined here because the reply policy needs them for IBKR's no-market-data warning; Session 4a's
pre-trade check reuses them.

* **Quote policy.** ``realtime_required`` (options): only a fresh real-time quote, on the option
  and on its underlying. ``realtime_preferred`` (stocks, ETFs, ETCs, bonds): a fresh real-time
  quote, or a fresh delayed one no later than ``max_delay_seconds``. ``nav_priced`` (funds): no
  quote policy applies. An asset class no rule names has none, and is refused. A quote is fresh
  when it was read at most ``max_quote_age_seconds`` ago.
* **Collar.** How far a limit may reach through the reference price, as a fraction of it:
  ``base + k·sigma·√(delay)``, the delay in years of trading time. A buy's limit may sit at most
  that far above the reference and a sell's at most that far below; a passive limit is never
  refused by it. A sell stop-limit's stop must rest below the band, so a stale quote cannot hide
  a stop that is already through the market. A delayed quote with no sigma cannot be widened
  for, and is refused; a collar past ``max_fraction`` means an implausible sigma, and refuses.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..config.schema import ObliskConfig
from ..config.schema_execution import (
    NavPriced,
    QuotePolicyRule,
    RealtimePreferred,
    RealtimeRequired,
)
from ..domain.enums import AssetClass, MarketDataStatus
from ..domain.orders import OrderSide, OrderTerms, QuoteEvidence

SECONDS_PER_HOUR = 3600


@dataclass(frozen=True, slots=True)
class QuoteCheck:
    """A verdict and its reason; ``ok`` is never assumed."""

    ok: bool
    reason: str


def _refuse(reason: str) -> QuoteCheck:
    return QuoteCheck(ok=False, reason=reason)


class QuotePolicy:
    """The quote policy and the collar, from ``execution.quote_policy`` and ``execution.collar``."""

    def __init__(self, settings: ObliskConfig) -> None:
        execution = settings.execution
        self._rules: dict[AssetClass, QuotePolicyRule] = {
            asset_class: rule
            for rule in execution.quote_policy
            for asset_class in rule.asset_classes
        }
        collar = execution.collar
        self._base = collar.base_fraction
        self._max_fraction = Decimal(repr(collar.max_fraction))
        self._multiplier = collar.sigma_multiplier
        self._seconds_per_year = collar.sessions_per_year * collar.session_hours * SECONDS_PER_HOUR

    def rule_for(self, asset_class: AssetClass) -> QuotePolicyRule | None:
        return self._rules.get(asset_class)

    def _fresh(
        self, quote: QuoteEvidence | None, max_age: int, now: datetime, what: str
    ) -> QuoteCheck:
        if quote is None:
            return _refuse(f"no quote for the {what}")
        age = (now - quote.observed_at).total_seconds()
        if age > max_age:
            return _refuse(f"the {what}'s quote is stale ({age:.0f} s old, at most {max_age} s)")
        if age < 0:
            return _refuse(f"the {what}'s quote is stamped in the future")
        return QuoteCheck(ok=True, reason="fresh")

    def check(
        self,
        asset_class: AssetClass,
        quote: QuoteEvidence | None,
        *,
        now: datetime,
        underlying: QuoteEvidence | None = None,
        require_delayed: bool = False,
    ) -> QuoteCheck:
        """Whether ``quote`` is one an order on ``asset_class`` may rest on at ``now``.

        ``require_delayed`` asks for the delayed case alone (the no-market-data warning: a
        real-time quote there contradicts the broker and is refused).
        """
        rule = self.rule_for(asset_class)
        if rule is None:
            return _refuse(f"no quote policy covers {asset_class.value}")
        if isinstance(rule, NavPriced):
            return _refuse(f"{asset_class.value} is priced at NAV; no quote policy applies")
        fresh = self._fresh(quote, rule.max_quote_age_seconds, now, "instrument")
        if not fresh.ok or quote is None:
            return fresh
        status = quote.status
        if isinstance(rule, RealtimeRequired):
            if require_delayed or status is not MarketDataStatus.REALTIME:
                return _refuse(f"{asset_class.value} requires a real-time quote")
            if asset_class is AssetClass.OPTION:
                fresh = self._fresh(underlying, rule.max_quote_age_seconds, now, "underlying")
                if not fresh.ok or underlying is None:
                    return fresh
                if underlying.status is not MarketDataStatus.REALTIME:
                    return _refuse("an option requires a real-time quote on its underlying")
            return QuoteCheck(ok=True, reason="real-time quote")
        return self._preferred(rule, quote, require_delayed=require_delayed)

    @staticmethod
    def _preferred(
        rule: RealtimePreferred, quote: QuoteEvidence, *, require_delayed: bool
    ) -> QuoteCheck:
        if quote.status is MarketDataStatus.REALTIME and not require_delayed:
            return QuoteCheck(ok=True, reason="real-time quote")
        if quote.status is not MarketDataStatus.DELAYED:
            label = "unknown" if quote.status is None else quote.status.value
            return _refuse(f"the quote is {label}, not a usable delayed quote")
        if quote.delay_seconds is None:
            return _refuse("the delayed quote does not say how late it is")
        if quote.delay_seconds > rule.max_delay_seconds:
            return _refuse(
                f"the quote is delayed {quote.delay_seconds} s, beyond {rule.max_delay_seconds} s"
            )
        return QuoteCheck(ok=True, reason=f"delayed quote ({quote.delay_seconds} s)")

    def collar_fraction(self, *, sigma: float, delay_seconds: int) -> Decimal:
        """``base + k·sigma·√(delay)``: the base alone for a real-time quote (no delay)."""
        widening = self._multiplier * sigma * math.sqrt(delay_seconds / self._seconds_per_year)
        return Decimal(repr(self._base + widening))

    def within_collar(
        self, terms: OrderTerms, quote: QuoteEvidence, *, sigma: float | None
    ) -> QuoteCheck:
        """Whether the order's limit (or a sell stop) sits inside the collar around ``quote``."""
        reference = quote.reference()
        if reference is None:
            return _refuse("the quote has no reference price")
        if quote.status is MarketDataStatus.REALTIME:
            fraction = self.collar_fraction(sigma=0.0, delay_seconds=0)
        elif quote.delay_seconds is None:
            return _refuse("the quote does not say how late it is")
        elif sigma is None:
            return _refuse("a delayed quote needs a volatility to widen the collar; none given")
        else:
            fraction = self.collar_fraction(sigma=sigma, delay_seconds=quote.delay_seconds)
        if fraction > self._max_fraction:
            return _refuse(
                f"the collar {fraction} would exceed its ceiling {self._max_fraction}: the "
                "volatility is not plausible"
            )
        upper = reference * (1 + fraction)
        lower = reference * (1 - fraction)
        price = terms.price
        if price.kind == "stop_limit":
            if terms.side is not OrderSide.SELL:
                return _refuse("a buy stop-limit has no collar rule")
            if price.stop > lower:
                return _refuse(f"the stop {price.stop} is inside the collar's band (below {lower})")
            return QuoteCheck(ok=True, reason=f"the stop rests below the band ({fraction})")
        if terms.side is OrderSide.BUY and price.limit > upper:
            return _refuse(f"the buy limit {price.limit} is above the collar ({upper})")
        if terms.side is OrderSide.SELL and price.limit < lower:
            return _refuse(f"the sell limit {price.limit} is below the collar ({lower})")
        return QuoteCheck(ok=True, reason=f"inside the collar ({fraction})")
