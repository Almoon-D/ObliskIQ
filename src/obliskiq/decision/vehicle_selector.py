"""The vehicle selector: an exposure's eligible vehicles, ranked with reasons (MASTER_PLAN §5.8).

An **exposure** is a core role (``core.roles``) or a grid cell (``market/sector``); its candidates
are the universe instruments that declare it. Each candidate passes the **gates** in order, and the
first that refuses it is its rejection, with the reason:

1. a tradable candidate of the universe (a data-only instrument says why it is one);
2. no prohibited product (a leveraged or inverse description, ``instruments``);
3. pinned and described (an instrument nobody can describe is never ordered);
4. eligible under the jurisdiction's policy (``EligibilityPolicy``);
5. a qualification rule for its vehicle type that it meets: shares, ETFs and ETCs qualify as
   listed products the order path has a quote policy for; bonds, mutual funds and baskets join
   with their own rules (Session 19a, DECISIONS D66) and until then are refused as such;
6. a currency-hedged share class only once the owner allows hedged classes
   (``vehicles.hedged_share_classes``): a hedged class is its own vehicle, ranked and refused on
   its own, never folded into its unhedged sibling (DECISIONS D69);
7. the broker's capability verified on its venue: search, quote and an accepted order preview,
   each read on the Client Portal route (:mod:`.capabilities`). An unverified capability is never
   proposed.

The vehicles that pass are **ranked** by a weighted score (``vehicles.ranking.weights``), each
criterion scored 0 to 1 with its reason:

* **cost**: the all-in cost a year of holding the reference amount — the fund's ongoing charge,
  plus a round trip (spread, slippage, commission and transaction tax from the cost model,
  :class:`~obliskiq.quant.costs.CostBook`) and its two currency conversions spread over
  ``holding_years``, plus the real-time market data the quote policy would need where the plan
  does not cover the venue; a cost that cannot be priced scores 0 and says why;
* **tracking**: the correlation of the vehicle's returns with the exposure's data series, each
  period of ``vehicles.correlation``, measured on research history by the caller
  (:mod:`obliskiq.research.tracking`);
* **liquidity**: the average daily traded value over the reference amount, in decades;
* **tax**: the share class under the active TaxPolicy (an accumulating class defers income where
  the policy taxes a fund's retained income only on sale);
* **currency**: trading in the base currency needs no conversion;
* **trading window**: how many of the day's decision cycles fall inside the venue's order window;
* **market data**: whether the owner's plan streams the venue in real time.

A criterion without its input (no research history, no stored bars) scores 0 and is labelled
unmeasured: honest degradation, never a guess. The selector is pure: the caller reads the store,
the research history and the calendars and passes them in.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol

from ..calendars.service import Calendars
from ..config.schema import ObliskConfig
from ..config.schema_execution import RealtimeRequired
from ..config.schema_universe import FUND_CLASSES, UniverseInstrument
from ..domain.enums import AssetClass, ShareClass
from ..errors import ObliskError
from ..execution.instruments import prohibited_reasons, traded_instrument
from ..jurisdiction.eligibility import EligibilityPolicy
from ..jurisdiction.tax import TaxPolicy
from ..orchestration.schedule import moment
from ..quant.costs import FX_VENUE, CostBook, LegCost
from ..quant.costs import OrderTerms as CostTerms
from ..quant.tracking import CorrelationReading
from .capabilities import BrokerCapabilities

ZERO: Final = Decimal(0)
ONE: Final = Decimal(1)
#: A round trip converts twice: into the vehicle's currency to buy, back to sell.
CONVERSIONS_PER_ROUND_TRIP: Final = 2
#: A year of a monthly price.
MONTHS_PER_YEAR: Final = 12


class UnknownExposureError(ObliskError):
    """The text names neither a core role nor a grid cell."""


class ExposureKind(StrEnum):
    CORE_ROLE = "core_role"
    GRID_CELL = "grid_cell"


@dataclass(frozen=True, slots=True)
class Exposure:
    """What a vehicle is chosen to hold. ``series`` is a core role's data series; a grid cell is
    measured by ``measured_by``, the instrument its tracking is read against."""

    kind: ExposureKind
    key: str
    name: str
    series: str | None
    measured_by: str | None

    def held_by(self, declared: UniverseInstrument) -> bool:
        if self.kind is ExposureKind.CORE_ROLE:
            return self.key in declared.exposures.core_roles
        grid = declared.exposures.grid
        return grid is not None and f"{grid.market}/{grid.sector}" == self.key


def resolve_exposure(settings: ObliskConfig, text: str) -> Exposure:
    """``text`` as an exposure: a core role's id, or a grid cell written ``market/sector``."""
    key = text.strip()
    role = settings.core.roles.get(key)
    if role is not None:
        return Exposure(ExposureKind.CORE_ROLE, key, role.name, role.exposure_series, None)
    market, slash, sector = key.partition("/")
    cell = settings.universe.cells().get((market, sector)) if slash else None
    if cell is not None:
        name = f"{market} {sector}" + ("" if cell.symbol else f" (unmapped: {cell.reason})")
        return Exposure(ExposureKind.GRID_CELL, key, name, None, cell.symbol)
    roles = ", ".join(sorted(settings.core.roles))
    raise UnknownExposureError(
        f"{text!r} is neither a core role ({roles}) nor a grid cell written market/sector"
    )


# ── inputs ──


@dataclass(frozen=True, slots=True)
class VehicleMarket:
    """What the market store holds of a vehicle: its latest close and its average daily traded
    value over the configured sessions, both in its listing currency."""

    price: Decimal | None
    average_daily_value: Decimal | None
    sessions: int


@dataclass(frozen=True, slots=True)
class WindowOverlap:
    """How many of a day's decision cycles fall inside a venue's order window."""

    inside: int
    cycles: int
    detail: str


@dataclass(frozen=True, slots=True)
class SelectorInputs:
    """Everything the ranking reads, gathered by the caller.

    ``rates``: the base currency's value of one unit of each currency (the base itself is 1).
    ``tracking``: each vehicle's correlation with the exposure's series, by symbol. ``windows``:
    each venue's overlap with the decision cycles, by MIC. ``market``: by conid.
    """

    as_of: date
    rates: Mapping[str, Decimal]
    tracking: Mapping[str, CorrelationReading] = field(default_factory=dict)
    windows: Mapping[str, WindowOverlap] = field(default_factory=dict)
    market: Mapping[int, VehicleMarket] = field(default_factory=dict)


# ── qualification rules ──


@dataclass(frozen=True, slots=True)
class Qualification:
    qualifies: bool
    reason: str


class QualificationRule(Protocol):
    """Whether a declared instrument qualifies as a vehicle of its type."""

    def qualify(self, declared: UniverseInstrument) -> Qualification: ...


class ListedProductRule:
    """A share, an ETF or an ETC: listed and quoted on its venue. It qualifies when the order
    path has a quote policy for its asset class (an order without one is refused at the gate)."""

    def __init__(self, settings: ObliskConfig) -> None:
        self._policies = {
            asset_class: rule.policy
            for rule in settings.execution.quote_policy
            for asset_class in rule.asset_classes
        }

    def qualify(self, declared: UniverseInstrument) -> Qualification:
        kind = declared.asset_class
        policy = self._policies.get(kind)
        if policy is None:
            return Qualification(False, f"no quote policy names {kind.value}, so none is ordered")
        return Qualification(True, f"a listed {kind.value} quoted under {policy}")


#: The vehicle types this session qualifies; bonds, mutual funds and baskets add theirs (19a).
LISTED_PRODUCTS: Final = (AssetClass.STOCK, AssetClass.ETF, AssetClass.ETC)


def default_rules(settings: ObliskConfig) -> dict[AssetClass, QualificationRule]:
    rule = ListedProductRule(settings)
    return dict.fromkeys(LISTED_PRODUCTS, rule)


# ── results ──


class Gate(StrEnum):
    NOT_TRADABLE = "not_tradable"
    PROHIBITED = "prohibited"
    NOT_DESCRIBED = "not_described"
    INELIGIBLE = "ineligible"
    NO_QUALIFICATION_RULE = "no_qualification_rule"
    NOT_QUALIFIED = "not_qualified"
    HEDGE_NOT_ALLOWED = "hedge_not_allowed"
    CAPABILITY_UNVERIFIED = "capability_unverified"


class Criterion(StrEnum):
    COST = "cost"
    TRACKING = "tracking"
    LIQUIDITY = "liquidity"
    TAX = "tax"
    CURRENCY = "currency"
    TRADING_WINDOW = "trading_window"
    MARKET_DATA = "market_data"


@dataclass(frozen=True, slots=True)
class Score:
    """One criterion's score (0 to 1), its weight, whether its input was measured, and why."""

    criterion: Criterion
    score: Decimal
    weight: Decimal
    measured: bool
    reason: str

    @property
    def contribution(self) -> Decimal:
        return self.score * self.weight


@dataclass(frozen=True, slots=True)
class Rejection:
    symbol: str
    gate: Gate
    reason: str


@dataclass(frozen=True, slots=True)
class RankedVehicle:
    """A vehicle that passed every gate, its score and every reason behind it."""

    symbol: str
    conid: int
    venue: str
    currency: str
    asset_class: AssetClass
    hedged: bool
    score: Decimal
    scores: tuple[Score, ...]
    all_in_cost: Decimal | None
    passed: tuple[str, ...]

    def criterion(self, name: Criterion) -> Score:
        return next(score for score in self.scores if score.criterion is name)


@dataclass(frozen=True, slots=True)
class Selection:
    exposure: Exposure
    ranked: tuple[RankedVehicle, ...]
    rejected: tuple[Rejection, ...]

    @property
    def best(self) -> RankedVehicle | None:
        return self.ranked[0] if self.ranked else None


# ── the trading window ──


def window_overlap(
    settings: ObliskConfig, calendars: Calendars, venue: str, day: date
) -> WindowOverlap:
    """How many of ``day``'s decision cycles fall inside ``venue``'s order window that day."""
    windows = calendars.tradable_windows(venue, day)
    moments: list[tuple[str, datetime]] = []
    for cycle in settings.runtime.cycles:
        at = moment(calendars, cycle.venue, cycle.anchor, cycle.offset_minutes, day)
        if at is not None:
            moments.append((cycle.name, at))
    inside = [name for name, at in moments if any(w.contains(at) for w in windows)]
    detail = f"{len(inside)} of {len(moments)} cycles on {day} inside its order window" + (
        f" ({', '.join(inside)})" if inside else ""
    )
    return WindowOverlap(len(inside), len(moments), detail)


# ── the selector ──


def _clamp(value: Decimal) -> Decimal:
    return max(ZERO, min(ONE, value))


@dataclass(frozen=True, slots=True)
class _Cost:
    annual: Decimal | None
    detail: str


class VehicleSelector:
    """Ranks an exposure's vehicles; see the module docstring for the gates and criteria."""

    def __init__(
        self,
        settings: ObliskConfig,
        eligibility: EligibilityPolicy,
        tax: TaxPolicy,
        *,
        rules: Mapping[AssetClass, QualificationRule] | None = None,
        capabilities: BrokerCapabilities | None = None,
        costs: CostBook | None = None,
    ) -> None:
        self._settings = settings
        self._eligibility = eligibility
        self._tax = tax
        self._rules = dict(default_rules(settings) if rules is None else rules)
        self._capabilities = capabilities or BrokerCapabilities(settings)
        self._costs = costs or CostBook.from_config(settings)
        self._ranking = settings.vehicles.ranking
        self._base = settings.account.base_currency

    def candidates(self, exposure: Exposure) -> tuple[UniverseInstrument, ...]:
        return tuple(i for i in self._settings.universe.instruments if exposure.held_by(i))

    def select(self, exposure: Exposure, inputs: SelectorInputs) -> Selection:
        ranked: list[RankedVehicle] = []
        rejected: list[Rejection] = []
        for declared in self.candidates(exposure):
            outcome = self._gate(declared)
            if isinstance(outcome, Rejection):
                rejected.append(outcome)
            else:
                ranked.append(self._rank(declared, outcome, inputs))
        ranked.sort(key=lambda vehicle: (-vehicle.score, vehicle.symbol))
        return Selection(exposure, tuple(ranked), tuple(rejected))

    # ── gates ──

    def venue_of(self, declared: UniverseInstrument) -> str:
        return self._settings.ibkr_exchanges[declared.listing_exchange].venue

    def _gate(self, declared: UniverseInstrument) -> Rejection | tuple[str, ...]:
        symbol = declared.symbol
        if not declared.tradable:
            return Rejection(symbol, Gate.NOT_TRADABLE, f"data only: {declared.data_only_reason}")
        traded = traded_instrument(declared)
        if traded is None:
            missing = (
                "not pinned to a conid" if declared.conid is None else "no resolved description"
            )
            return Rejection(symbol, Gate.NOT_DESCRIBED, f"{missing}: never ordered until it is")
        prohibited = prohibited_reasons(traded, None, self._settings)
        if prohibited:
            return Rejection(symbol, Gate.PROHIBITED, "; ".join(prohibited))
        verdict = self._eligibility.assess_declared(declared)
        if not verdict.eligible:
            return Rejection(symbol, Gate.INELIGIBLE, verdict.reason)
        rule = self._rules.get(declared.asset_class)
        if rule is None:
            return Rejection(
                symbol,
                Gate.NO_QUALIFICATION_RULE,
                f"no qualification rule for {declared.asset_class.value} vehicles yet (bonds, "
                "mutual funds and baskets qualify by their own rules, Session 19a)",
            )
        qualification = rule.qualify(declared)
        if not qualification.qualifies:
            return Rejection(symbol, Gate.NOT_QUALIFIED, qualification.reason)
        hedge = self._hedge_refusal(declared)
        if hedge is not None:
            return Rejection(symbol, Gate.HEDGE_NOT_ALLOWED, hedge)
        capability = self._capabilities.assess(declared.asset_class, self.venue_of(declared))
        if not capability.verified:
            return Rejection(symbol, Gate.CAPABILITY_UNVERIFIED, capability.reason)
        return (verdict.reason, qualification.reason, capability.reason)

    def _hedge_refusal(self, declared: UniverseInstrument) -> str | None:
        """Why a packaged product's share class may not be used: its hedge is unknown, or it
        hedges and the owner has not allowed hedged classes. A share has no class to hedge."""
        attributes = declared.attributes
        ruling = self._settings.vehicles.hedged_share_classes
        if declared.asset_class not in FUND_CLASSES:
            return None
        if attributes.hedged is None:
            return "whether the share class hedges its currency is unknown"
        if not attributes.hedged or ruling == "allowed":
            return None
        return (
            f"a share class hedged to {attributes.hedge_currency}: the owner has "
            + ("not ruled" if ruling == "undecided" else "ruled against")
            + " whether a hedge inside a UCITS share class fits the mandate "
            "(vehicles.hedged_share_classes, MASTER_PLAN §11)"
        )

    # ── ranking ──

    def _rank(
        self, declared: UniverseInstrument, passed: tuple[str, ...], inputs: SelectorInputs
    ) -> RankedVehicle:
        weights = self._settings.vehicles.ranking.weights
        venue = self.venue_of(declared)
        market = inputs.market.get(declared.conid or 0)
        cost = self._cost(declared, venue, market, inputs)
        scores = (
            self._cost_score(cost, weights.cost),
            self._tracking_score(declared, inputs, weights.tracking),
            self._liquidity_score(declared, market, inputs, weights.liquidity),
            self._tax_score(declared, weights.tax),
            self._currency_score(declared, weights.currency),
            self._window_score(venue, inputs, weights.trading_window),
            self._market_data_score(declared, venue, weights.market_data),
        )
        total = sum((score.contribution for score in scores), ZERO)
        return RankedVehicle(
            symbol=declared.symbol,
            conid=declared.conid or 0,
            venue=venue,
            currency=declared.currency,
            asset_class=declared.asset_class,
            hedged=bool(declared.attributes.hedged),
            score=total,
            scores=scores,
            all_in_cost=cost.annual,
            passed=passed,
        )

    def _rate(self, currency: str, inputs: SelectorInputs) -> Decimal | None:
        return ONE if currency == self._base else inputs.rates.get(currency)

    def _cost(
        self,
        declared: UniverseInstrument,
        venue: str,
        market: VehicleMarket | None,
        inputs: SelectorInputs,
    ) -> _Cost:
        amount_base = self._ranking.reference_amount_base
        currency = declared.currency
        gaps: list[str] = []
        fee = declared.attributes.fee
        if fee is None:
            gaps.append("the ongoing charge is unknown")
        rate = self._rate(currency, inputs)
        if rate is None:
            return _Cost(None, f"no rate values {currency} in {self._base}")
        amount = amount_base / rate
        price = market.price if market is not None else None
        adv = market.average_daily_value if market is not None else None
        terms = CostTerms(
            venue=venue,
            asset_class=declared.asset_class,
            currency=currency,
            quantity=amount / (price or ONE),
            price=price or ONE,
        )
        if price is None and self._per_share(terms):
            gaps.append("no stored price, and its commission is charged per share")
        issuer = declared.exposures.country if declared.asset_class is AssetClass.STOCK else None
        legs = [
            self._costs.leg_cost(
                terms, side=side, on=inputs.as_of, issuer_country=issuer, average_daily_value=adv
            )
            for side in ("buy", "sell")
        ]
        for leg in legs:
            reason = self._leg_gap(leg) if leg.total is None else None
            if reason is not None and reason not in gaps:
                gaps.append(reason)
        conversion = self._conversion(currency, inputs, gaps)
        market_data = self._market_data_cost(declared, venue, inputs, gaps)
        if gaps or fee is None:
            return _Cost(None, "; ".join(gaps))
        trading = sum((leg.total or ZERO for leg in legs), ZERO) / amount
        annual = (
            fee
            + (trading + CONVERSIONS_PER_ROUND_TRIP * conversion) / self._ranking.holding_years
            + market_data
        )
        labels = sorted({label for leg in legs for label in leg.unverified})
        detail = (
            f"{annual:.4%} a year at {amount_base} {self._base}: fee {fee:.2%}, round trip "
            f"{trading:.3%} and conversions {CONVERSIONS_PER_ROUND_TRIP * conversion:.3%} over "
            f"{self._ranking.holding_years} years, market data {market_data:.3%}"
            + (f" [unverified: {', '.join(labels)}]" if labels else "")
        )
        return _Cost(annual, detail)

    def _per_share(self, terms: CostTerms) -> bool:
        estimate = self._costs.commission(terms)
        kinds = {schedule.id: schedule.kind for schedule in self._settings.costs.commissions}
        return estimate.schedule_id is not None and kinds[estimate.schedule_id] == "per_share"

    @staticmethod
    def _leg_gap(leg: LegCost) -> str:
        details = {leg.commission.gap: leg.commission.detail, leg.tax.gap: leg.tax.detail}
        return "; ".join(details.get(gap, gap.value) for gap in leg.gaps)

    def _conversion(self, currency: str, inputs: SelectorInputs, gaps: list[str]) -> Decimal:
        """One conversion's commission as a fraction of the reference amount."""
        if currency == self._base:
            return ZERO
        schedules = [
            s for s in self._settings.costs.commissions if AssetClass.FX in s.asset_classes
        ]
        if not schedules:
            gaps.append("no commission schedule prices a currency conversion")
            return ZERO
        quoted = schedules[0].currency
        rate = self._rate(quoted, inputs)
        if rate is None:
            gaps.append(f"no rate values {quoted} in {self._base} to cost the conversion")
            return ZERO
        amount = self._ranking.reference_amount_base / rate
        terms = CostTerms(
            venue=FX_VENUE, asset_class=AssetClass.FX, currency=quoted, quantity=amount, price=ONE
        )
        estimate = self._costs.commission(terms)
        if estimate.amount is None:
            gaps.append(f"the conversion cannot be priced: {estimate.detail}")
            return ZERO
        return estimate.amount / amount

    def _quote_policy(self, asset_class: AssetClass) -> str | None:
        return next(
            (
                rule.policy
                for rule in self._settings.execution.quote_policy
                if asset_class in rule.asset_classes
            ),
            None,
        )

    def _market_data_cost(
        self, declared: UniverseInstrument, venue: str, inputs: SelectorInputs, gaps: list[str]
    ) -> Decimal:
        """A year of the real-time feed the quote policy needs where the plan lacks it, as a
        fraction of the reference amount."""
        plan = self._settings.market_data
        kind = declared.asset_class
        if plan.planned_feed(venue, kind) is not None:
            return ZERO
        rules = [r for r in self._settings.execution.quote_policy if kind in r.asset_classes]
        if not any(isinstance(rule, RealtimeRequired) for rule in rules):
            return ZERO
        offered = [f for f in plan.feeds if venue in f.venues and kind in f.asset_classes]
        priced = [
            (feed.monthly_cost.amount * rate, feed.id)
            for feed in offered
            if (rate := self._rate(feed.monthly_cost.currency, inputs)) is not None
        ]
        if not priced:
            gaps.append(f"real-time {kind.value} data on {venue} is required and no feed is priced")
            return ZERO
        monthly, _ = min(priced)
        return monthly * MONTHS_PER_YEAR / self._ranking.reference_amount_base

    def _cost_score(self, cost: _Cost, weight: Decimal) -> Score:
        if cost.annual is None:
            return Score(Criterion.COST, ZERO, weight, False, f"cannot be priced: {cost.detail}")
        score = _clamp(ONE - cost.annual / self._ranking.cost_ceiling)
        return Score(Criterion.COST, score, weight, True, cost.detail)

    def _tracking_score(
        self, declared: UniverseInstrument, inputs: SelectorInputs, weight: Decimal
    ) -> Score:
        reading = inputs.tracking.get(declared.symbol)
        if reading is None or reading.value is None:
            why = "no reading was taken" if reading is None else reading.reason
            return Score(Criterion.TRACKING, ZERO, weight, False, f"unmeasured: {why}")
        floor = self._ranking.tracking_floor
        value = Decimal(repr(reading.value))
        score = _clamp((value - floor) / (ONE - floor))
        return Score(Criterion.TRACKING, score, weight, True, f"correlation {reading.describe()}")

    def _liquidity_score(
        self,
        declared: UniverseInstrument,
        market: VehicleMarket | None,
        inputs: SelectorInputs,
        weight: Decimal,
    ) -> Score:
        adv = market.average_daily_value if market is not None else None
        rate = self._rate(declared.currency, inputs)
        if adv is None or adv <= 0 or rate is None:
            why = "no stored daily bars with volume" if adv is None or adv <= 0 else "no rate"
            return Score(Criterion.LIQUIDITY, ZERO, weight, False, f"unmeasured: {why}")
        ratio = adv * rate / self._ranking.reference_amount_base
        score = _clamp(ratio.log10() / self._ranking.liquidity_decades) if ratio > ONE else ZERO
        sessions = market.sessions if market is not None else 0
        return Score(
            Criterion.LIQUIDITY,
            score,
            weight,
            True,
            f"average daily value {adv * rate:,.0f} {self._base} over {sessions} sessions, "
            f"{ratio:,.0f} times the reference amount",
        )

    def _tax_score(self, declared: UniverseInstrument, weight: Decimal) -> Score:
        share = declared.attributes.share_class
        policy = self._tax.policy_id
        if share is None:
            return Score(Criterion.TAX, ZERO, weight, False, "unmeasured: share class unknown")
        if not self._tax.retained_income_deferred:
            return Score(
                Criterion.TAX,
                ONE,
                weight,
                True,
                f"{policy} taxes retained and distributed income alike",
            )
        if share is ShareClass.ACCUMULATING:
            what = (
                "pays no income: its return is"
                if declared.asset_class is AssetClass.ETC
                else "accumulating: the income it retains is"
            )
            return Score(
                Criterion.TAX, ONE, weight, True, f"{what} taxed only when it is sold ({policy})"
            )
        return Score(
            Criterion.TAX,
            ZERO,
            weight,
            True,
            f"distributing: each distribution is taxed when paid ({policy})",
        )

    def _currency_score(self, declared: UniverseInstrument, weight: Decimal) -> Score:
        currency = declared.currency
        hedge = declared.attributes.hedge_currency
        hedged = f"; its class hedges to {hedge}" if hedge else ""
        if currency == self._base:
            return Score(
                Criterion.CURRENCY,
                ONE,
                weight,
                True,
                f"trades in {currency}, the base currency: no conversion{hedged}",
            )
        return Score(
            Criterion.CURRENCY,
            ZERO,
            weight,
            True,
            f"trades in {currency}: each purchase and sale converts (costed){hedged}",
        )

    def _window_score(self, venue: str, inputs: SelectorInputs, weight: Decimal) -> Score:
        overlap = inputs.windows.get(venue)
        if overlap is None or overlap.cycles == 0:
            why = "no calendar reading" if overlap is None else overlap.detail
            return Score(Criterion.TRADING_WINDOW, ZERO, weight, False, f"unmeasured: {why}")
        score = Decimal(overlap.inside) / Decimal(overlap.cycles)
        return Score(Criterion.TRADING_WINDOW, score, weight, True, overlap.detail)

    def _market_data_score(
        self, declared: UniverseInstrument, venue: str, weight: Decimal
    ) -> Score:
        kind = declared.asset_class
        feed = self._settings.market_data.planned_feed(venue, kind)
        if feed is not None:
            return Score(
                Criterion.MARKET_DATA, ONE, weight, True, f"real time on the plan's {feed.id}"
            )
        policy = self._quote_policy(kind) or "no quote policy"
        return Score(
            Criterion.MARKET_DATA,
            ZERO,
            weight,
            True,
            f"delayed: the plan streams no {kind.value} on {venue} ({policy})",
        )
