"""Pricing an order: an IBKR algo with a limit cap, or a patient limit repriced toward the touch
(MASTER_PLAN §5.12).

* **An algo** where ``execution.pricing.algos`` holds an enabled rule for the instrument's
  listing venue and asset class and the contract's algo list (``/iserver/contract/{conid}/algos``)
  offers its strategy: the algo works the order, capped by a limit at the collar's far edge,
  rounded inward to a whole tick. Every rule ships off until a paper order verifies it.
* **Otherwise a patient limit**: ``start_ticks_inside_mid`` ticks inside the reference (below it
  for a buy, above it for a sell), rounded to the contract's tick away from the touch.
* **Repricing** moves the limit toward the touch (the ask for a buy, the bid for a sell) by an
  equal share of what is left, so the last of ``max_reprices`` steps reaches it; never past the
  touch or the collar, which widens with sigma times the root of the delay for a delayed quote.
  Each reprice is a modify the pre-trade gate clears afresh (the executor sends it); after the
  last one the order is cancelled and retried at the next tradable window.
* **Never a plain market order**: every price this module returns carries a limit.
* **Venues.** Orders route SMART (DECISIONS D97); the cost model is given the instrument's listing
  venue (:func:`cost_venue`), never ``SMART``.

The tick comes from the contract's trading rules: the increment of the rule with the highest
lower edge at or below the price.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..config.schema_execution import AlgoRule
from ..domain.instrument import TradedInstrument
from ..domain.orders import (
    AlgoLimitPrice,
    AlgoParameter,
    LimitPrice,
    OrderSide,
    PricePolicy,
    QuoteEvidence,
)
from ..ibkr.models import Algo, ContractRules
from .quotes import CollarBand, QuoteCheck, QuotePolicy


def tick_for(rules: ContractRules, price: Decimal) -> Decimal | None:
    """The price increment that applies at ``price``: the rule with the highest lower edge at
    or below it."""
    applicable = [rule for rule in rules.increment_rules if Decimal(repr(rule.lower_edge)) <= price]
    if not applicable:
        return None
    return Decimal(repr(max(applicable, key=lambda rule: rule.lower_edge).increment))


def to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    """``price`` on a whole tick: rounded up, or down."""
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return (price / tick).to_integral_value(rounding=rounding) * tick


def cost_venue(instrument: TradedInstrument, calendars: Calendars) -> str:
    """The venue the cost model prices an order on: the listing venue's MIC, never SMART."""
    return calendars.venue_for_ibkr(instrument.listing_exchange)


@dataclass(frozen=True, slots=True)
class Priced:
    """A price for an order, or why there is none (``price`` ``None``)."""

    price: PricePolicy | None
    detail: str


class Pricer:
    """Prices entries and exits from a quote, the contract's rules and its algos."""

    def __init__(self, settings: ObliskConfig, quotes: QuotePolicy | None = None) -> None:
        pricing = settings.execution.pricing
        self._algos = tuple(rule for rule in pricing.algos if rule.enabled)
        self._start_ticks = pricing.start_ticks_inside_mid
        self._max_reprices = pricing.max_reprices
        self._quotes = quotes if quotes is not None else QuotePolicy(settings)

    @property
    def max_reprices(self) -> int:
        return self._max_reprices

    def _algo(
        self, instrument: TradedInstrument, venue: str, offered: Sequence[Algo]
    ) -> AlgoRule | None:
        ids = {algo.algo_id for algo in offered}
        return next(
            (
                rule
                for rule in self._algos
                if venue in rule.venues
                and instrument.asset_class in rule.asset_classes
                and rule.strategy in ids
            ),
            None,
        )

    def _band(self, quote: QuoteEvidence, sigma: float | None) -> CollarBand | str:
        band = self._quotes.band(quote, sigma=sigma)
        return band.reason if isinstance(band, QuoteCheck) else band

    def initial(
        self,
        instrument: TradedInstrument,
        side: OrderSide,
        quote: QuoteEvidence,
        *,
        sigma: float | None,
        rules: ContractRules,
        algos: Sequence[Algo],
        venue: str,
    ) -> Priced:
        """The first price of an order on ``instrument`` listed on ``venue`` (a MIC)."""
        band = self._band(quote, sigma)
        if isinstance(band, str):
            return Priced(None, band)
        tick = tick_for(rules, band.reference)
        if tick is None:
            return Priced(None, "the contract's rules give no tick at this price")
        buying = side is OrderSide.BUY
        rule = self._algo(instrument, venue, algos)
        if rule is not None:
            cap = to_tick(band.upper if buying else band.lower, tick, up=not buying)
            parameters = tuple(AlgoParameter(name=p.name, value=p.value) for p in rule.parameters)
            return Priced(
                AlgoLimitPrice(strategy=rule.strategy, limit=cap, parameters=parameters),
                f"{rule.strategy} capped at {cap}",
            )
        inside = tick * self._start_ticks
        start = band.reference - inside if buying else band.reference + inside
        limit = to_tick(start, tick, up=not buying)
        if limit <= 0:
            return Priced(None, f"a patient limit {limit} is not a price")
        return Priced(LimitPrice(limit=limit), f"patient limit {limit} ({inside} inside the mid)")

    def reprice(
        self,
        side: OrderSide,
        current: Decimal,
        quote: QuoteEvidence,
        *,
        sigma: float | None,
        rules: ContractRules,
        done: int,
    ) -> Priced:
        """The next limit toward the touch after ``done`` reprices; ``price`` ``None`` when no
        reprice is left or the limit would not move toward the touch."""
        remaining = self._max_reprices - done
        if remaining <= 0:
            return Priced(None, f"all {self._max_reprices} reprices are spent")
        band = self._band(quote, sigma)
        if isinstance(band, str):
            return Priced(None, band)
        buying = side is OrderSide.BUY
        touch = (quote.ask if buying else quote.bid) or band.reference
        tick = tick_for(rules, touch)
        if tick is None:
            return Priced(None, "the contract's rules give no tick at this price")
        step = current + (touch - current) / remaining
        if buying:
            limit = min(to_tick(step, tick, up=True), to_tick(band.upper, tick, up=False))
            moved = limit > current
        else:
            limit = max(to_tick(step, tick, up=False), to_tick(band.lower, tick, up=True))
            moved = limit < current
        if not moved:
            return Priced(None, f"the touch {touch} gives no better limit than {current}")
        return Priced(LimitPrice(limit=limit), f"reprice {done + 1} of {self._max_reprices}")
