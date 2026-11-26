"""The idea ledger's grading (research/preregistrations/idea-ledger.md, H14; DECISIONS D187).

**A call** is an idea written into the ledger at the first cycle its own checks passed
(``store/ledger.py``). It is read from the **first close after the day it was written** (a close
of a later session, so nothing seen before the call counts) on the instrument's daily closes, in
its own currency against its own levels, and resolves at the first close at or beyond its target
(**target first**), at or beyond its invalidation (**invalidation first**), or at the last close
on or before its horizon (**horizon**). Until then it is **open**. Its outcome is its excess log
return over the equity reference in the base currency, signed by its view, less the cost model's
round trip (spread, slippage and commission, two legs, on a trade of the registered value). A call
without a close within the mark age after it was written, with two closes further apart than
that, without a mark of the reference, or whose round trip cannot be priced is **not
evaluable**: counted, never marked at a stale price.

**A group** (a segment, a source, an event tag) is read as the scoreboard reads a layer
(``grading.record``): weekly cohorts of the week a call was written, a Newey-West mean of as many
lags as its resolved calls were held weeks on average, the minimum in cohorts and effective
observations, and the D62 verdict against θ, Bonferroni-adjusted over the groups past the
minimum. **A segment's calibration** is the scoreboard's rule (``calibration.calibration_state``)
on its resolved calls that state a probability, against the segment's own base rate. Everything
is read as of a day: a close or a call known later changes nothing.

Nothing here sizes, moves capital or judges an idea: the one use of a grade is the order of the
book's free slots, and only for a segment whose calibration passed: the rebalancer reads the
stored ``ledger_segments`` row and ranks with ``engines.thesis.book.rank_score``.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Final, Literal

from ..config.schema import ObliskConfig
from ..domain.market import Bar
from ..errors import PreregistrationError
from ..quant.costs import CostBook
from ..quant.costs import OrderTerms as CostTerms
from ..research.prereg import CalibrationSpec, IdeaLedgerSpec
from ..store import Store, repos
from ..store import ledger as ledger_repo
from ..store import theses as thesis_repo
from ..store.ledger import IdeaCall, SegmentState
from ..store.scoreboard import Call
from .calibration import CalibrationState, calibration_state
from .grading import (
    RELIABILITY_BINS,
    GradedCall,
    ProbabilityScore,
    Record,
    iso_week,
    probability_score,
    record,
)
from .h13_run import VenueOf, market_value
from .rules import DAYS_PER_WEEK, GradingRules
from .run import StoreMarks

type Outcome = Literal["target", "invalidation", "horizon"]

OPEN: Final = "open: neither level crossed and its horizon not passed"
#: The group prefixes of a report.
SEGMENT: Final = "segment:"
SOURCE: Final = "source:"
EVENT: Final = "event:"
ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class CallPath:
    """Where a call's closes took it: ``outcome`` and the dates it started and resolved on, or why
    it has neither (``reason``: open, or not evaluable)."""

    outcome: Outcome | None
    start: date | None
    end: date | None
    reason: str | None
    start_close: Decimal | None = None
    end_close: Decimal | None = None

    @property
    def open(self) -> bool:
        return self.reason == OPEN


def call_path(call: IdeaCall, bars: Sequence[Bar], *, as_of: date, max_age_days: int) -> CallPath:
    """The closes after the day ``call`` was written, up to ``as_of`` and its horizon, read
    against its own levels. ``bars`` are the instrument's, oldest first."""
    written = call.known_at.date()
    age = timedelta(days=max_age_days)
    after = [bar for bar in bars if written < bar.session_date <= min(as_of, call.horizon)]
    if not after:
        if as_of < written + age and as_of < call.horizon:
            return CallPath(None, None, None, OPEN)
        return CallPath(
            None, None, None, f"no close within {max_age_days} days after it was written"
        )
    first = after[0]
    if first.session_date - written > age:
        return CallPath(
            None, None, None, f"no close within {max_age_days} days after it was written"
        )
    sign = Decimal(1) if call.rises else Decimal(-1)
    start = Decimal(repr(first.close))
    previous = first.session_date
    for bar in after:
        if bar.session_date - previous > age:
            return CallPath(
                None,
                first.session_date,
                None,
                f"no close between {previous.isoformat()} and {bar.session_date.isoformat()}: "
                "a level may have been crossed unseen",
            )
        previous = bar.session_date
        close = Decimal(repr(bar.close))
        crossed: Outcome | None = None
        if sign * (close - call.target) >= 0:
            crossed = "target"
        elif sign * (close - call.invalidation) <= 0:
            crossed = "invalidation"
        if crossed is not None:
            return CallPath(crossed, first.session_date, bar.session_date, None, start, close)
    if call.horizon > as_of:
        return CallPath(None, first.session_date, None, OPEN, start)
    last = after[-1]
    if call.horizon - last.session_date > age:
        return CallPath(
            None,
            first.session_date,
            None,
            f"no close within {max_age_days} days before its horizon {call.horizon.isoformat()}",
        )
    return CallPath(
        "horizon", first.session_date, last.session_date, None, start, Decimal(repr(last.close))
    )


# ── the round trip's cost ──


type CostOf = Callable[[IdeaCall, CallPath], float | str]


def cost_model(
    store: Store, settings: ObliskConfig, spec: IdeaLedgerSpec, venue_of: VenueOf
) -> CostOf:
    """The round trip's cost of a call as a share of its trade: two legs of the cost model's
    spread and slippage for the instrument's tier and of its commission, on a trade of the
    registered value in the base currency at the first close; or why it cannot be priced."""
    costs = CostBook.from_config(settings)
    marks = StoreMarks(
        store,
        base=settings.account.base_currency,
        max_age_days=spec.outcome.max_mark_age_days,
        as_of=date.max,
    )
    sessions = settings.thesis.liquidity.sessions

    def cost(call: IdeaCall, found: CallPath) -> float | str:
        # Only a resolved path is costed: it has both dates and both closes.
        assert found.start is not None
        assert found.end is not None
        assert found.start_close is not None
        assert found.end_close is not None
        described = repos.get_instrument(store, call.conid, as_of=found.start)
        if described is None:
            return f"{call.symbol} is not in the instrument store"
        venue = venue_of(described.listing_exchange)
        rate = marks.rate(described.currency, found.start)
        if venue is None or rate is None:
            missing = "venue" if venue is None else f"{described.currency} rate"
            return f"{call.symbol} has no {missing} to price its round trip"
        units = (spec.outcome.cost_trade_value / (found.start_close * rate)).to_integral_value(
            rounding=ROUND_FLOOR
        )
        units = max(units, Decimal(1))
        share = ZERO
        for side, close, day in (
            ("buy", found.start_close, found.start),
            ("sell", found.end_close, found.end),
        ):
            leg = costs.leg_cost(
                CostTerms(
                    venue=venue,
                    asset_class=described.asset_class,
                    currency=described.currency,
                    quantity=units,
                    price=close,
                ),
                side="buy" if side == "buy" else "sell",
                on=day,
                average_daily_value=market_value(store, call.conid, day, sessions),
            )
            commission = leg.commission.amount
            if (
                leg.spread_cost is None
                or leg.slippage_cost is None
                or commission is None
                or leg.notional is None
                or leg.notional <= ZERO
            ):
                gaps = ", ".join(gap.value for gap in leg.gaps) or "a component without a price"
                return f"the cost model cannot price {call.symbol}'s round trip ({gaps})"
            share += (leg.spread_cost + leg.slippage_cost + commission) / leg.notional
        return float(share)

    return cost


# ── grading ──


@dataclass(frozen=True, slots=True)
class Graded:
    """A call, where its closes took it, and its graded form (``excess`` ``None`` while open or
    when not evaluable)."""

    call: IdeaCall
    path: CallPath
    graded: GradedCall

    @property
    def resolved(self) -> bool:
        return self.graded.graded

    @property
    def held_days(self) -> int | None:
        if self.path.start is None or self.path.end is None:
            return None
        return (self.path.end - self.path.start).days


def grade(
    call: IdeaCall,
    found: CallPath,
    *,
    price: Callable[[int, date], Decimal | None],
    reference: int,
    cost: CostOf,
) -> Graded:
    """``call`` graded from its path: the signed excess log return in the base currency less the
    round trip's cost, and whether its target came first."""
    as_call = Call(
        layer=call.segment,
        conid=call.conid,
        direction="up" if call.rises else "down",
        called_at=call.known_at,
        detail=f"thesis {call.thesis_id}",
        probability=call.probability,
    )
    week = iso_week(call.known_at)

    def unresolved(reason: str) -> Graded:
        return Graded(call, found, GradedCall(as_call, week, None, reason))

    if found.outcome is None or found.start is None or found.end is None:
        return unresolved(found.reason or OPEN)
    p0, p1 = price(call.conid, found.start), price(call.conid, found.end)
    r0, r1 = price(reference, found.start), price(reference, found.end)
    if p0 is None or p1 is None:
        return unresolved(f"{call.symbol} has no mark in the base currency")
    if r0 is None or r1 is None:
        return unresolved("no mark of the equity reference within the age allowed")
    charged = cost(call, found)
    if isinstance(charged, str):
        return unresolved(charged)
    sign = 1.0 if call.rises else -1.0
    excess = sign * (math.log(p1 / p0) - math.log(r1 / r0)) - charged
    return Graded(
        call, found, GradedCall(as_call, week, excess, None, hit=found.outcome == "target")
    )


@dataclass(frozen=True, slots=True)
class Group:
    """A group's calls and its record (``tests``: the groups its interval is adjusted over)."""

    name: str
    calls: int
    open: int
    missing: int
    outcomes: dict[str, int]
    held_weeks: int
    record: Record


@dataclass(frozen=True, slots=True)
class Calibration:
    """A segment's stated probabilities against its outcomes."""

    segment: str
    state: CalibrationState
    base_rate: float | None
    score: ProbabilityScore
    #: Past the minimum: its score and reliability table may be read.
    mature: bool

    def stored(self, as_of: date) -> SegmentState:
        return SegmentState(
            segment=self.segment,
            as_of=as_of,
            passed=self.state.calibrated and self.base_rate is not None,
            shrink=self.state.shrink,
            base_rate=self.base_rate,
            reason=self.state.reason,
        )


@dataclass(frozen=True, slots=True)
class LedgerBook:
    """The ledger on a day: every group's record, each segment's calibration, and what is not in
    the record (calls not evaluable and why, calls inside an open one, ideas never written)."""

    as_of: date
    groups: dict[str, Group]
    calibration: dict[str, Calibration]
    tests: int
    not_evaluable: dict[str, int]
    #: Calls written while an earlier one of their segment on the instrument was still open.
    overlapping: int
    unwritten: int
    #: The registered minimums, in words: of a record, and of a segment's calibration.
    minimum: str = ""
    calibration_minimum: str = ""
    graded: list[Graded] = field(default_factory=list)

    def summary(self) -> str:
        trusted = sum(1 for found in self.calibration.values() if found.state.calibrated)
        return (
            f"idea ledger: {len(self.graded)} calls, "
            f"{sum(1 for g in self.graded if g.resolved)} resolved, {trusted} segments trusted"
        )


def _rules(spec: IdeaLedgerSpec, items: Sequence[Graded], target_power: float) -> GradingRules:
    held = [g.held_days for g in items if g.resolved and g.held_days is not None]
    mean_days = sum(held) / len(held) if held else 0
    weeks = max(1, math.ceil(mean_days / DAYS_PER_WEEK))
    statistic = spec.statistic
    return GradingRules(
        rho=float(statistic.rho),
        horizon_days=weeks * DAYS_PER_WEEK,
        alpha=float(statistic.alpha),
        theta=float(statistic.theta),
        min_cohorts=statistic.min_cohorts,
        min_effective=float(statistic.min_effective),
        max_mark_age_days=spec.outcome.max_mark_age_days,
        target_power=target_power,
    )


def _group(name: str, items: Sequence[Graded], rules: GradingRules, tests: int) -> Group:
    outcomes = {
        kind: sum(1 for g in items if g.resolved and g.path.outcome == kind)
        for kind in ("target", "invalidation", "horizon")
    }
    return Group(
        name=name,
        calls=len(items),
        open=sum(1 for g in items if g.path.open),
        missing=sum(1 for g in items if not g.resolved and not g.path.open),
        outcomes=outcomes,
        held_weeks=rules.horizon_weeks,
        record=record(name, [g.graded for g in items], rules, tests=tests),
    )


def _calibration(
    segment: str, items: Sequence[Graded], spec: IdeaLedgerSpec, rules: GradingRules
) -> Calibration:
    graded = [g.graded for g in items]
    stated = [g for g in graded if g.graded and g.call.probability is not None]
    base = sum(1 for g in stated if g.right) / len(stated) if stated else None
    registered = spec.calibration
    as_scoreboard = CalibrationSpec(
        min_cohorts=registered.min_cohorts,
        min_effective=registered.min_effective,
        brier_skill_above=registered.brier_skill_above,
        shrink=registered.shrink,
        shrink_alpha=registered.shrink_alpha,
    )
    state = calibration_state(segment, graded, rules, as_scoreboard)
    score = probability_score(graded, rules)
    mature = score.cohorts >= registered.min_cohorts and score.effective >= float(
        registered.min_effective
    )
    return Calibration(segment, state, base, score, mature)


def groups_of(call: IdeaCall) -> list[str]:
    return [
        f"{SEGMENT}{call.segment}",
        f"{SOURCE}{call.source.value}",
        *(f"{EVENT}{tag}" for tag in call.event_tags),
    ]


def _still_open(
    store: Store, call: IdeaCall, earlier: Sequence[Graded], *, max_age_days: int
) -> bool:
    """Whether an earlier call of ``call``'s segment on its instrument was still open on the day
    ``call`` was written: then ``call`` is not a call of its own (one open call per instrument and
    segment), read on what was known that day."""
    day = call.known_at.date()
    for item in earlier:
        before = item.call
        if before.conid != call.conid or before.segment != call.segment:
            continue
        bars = repos.bar_series(store, before.conid, as_of=min(day, before.horizon))
        if call_path(before, bars, as_of=day, max_age_days=max_age_days).open:
            return True
    return False


def grade_ledger(
    store: Store,
    settings: ObliskConfig,
    spec: IdeaLedgerSpec,
    *,
    as_of: date,
    venue_of: VenueOf,
) -> LedgerBook:
    """Every call known by the end of ``as_of``, graded on what was known by then."""
    if spec.calibration.reliability_bins != RELIABILITY_BINS:
        raise PreregistrationError(
            f"the ledger registers {spec.calibration.reliability_bins} reliability bins; the "
            f"scoreboard's table has {RELIABILITY_BINS}"
        )
    age = spec.outcome.max_mark_age_days
    marks = StoreMarks(store, base=settings.account.base_currency, max_age_days=age, as_of=as_of)
    reference = settings.portfolio_circuit_breaker.equity_reference_conid
    cost = cost_model(store, settings, spec, venue_of)
    target_power = float(settings.research.decision.target_power)
    graded: list[Graded] = []
    overlapping = 0
    for call in ledger_repo.calls(store, as_of=as_of):
        if _still_open(store, call, graded, max_age_days=age):
            overlapping += 1
            continue
        bars = repos.bar_series(
            store, call.conid, as_of=min(as_of, call.horizon), since=call.known_at.date()
        )
        found = call_path(call, bars, as_of=as_of, max_age_days=age)
        graded.append(grade(call, found, price=marks.price, reference=reference, cost=cost))
    by_group: dict[str, list[Graded]] = {}
    for item in graded:
        for name in groups_of(item.call):
            by_group.setdefault(name, []).append(item)
    rules = {name: _rules(spec, items, target_power) for name, items in by_group.items()}
    first = {name: _group(name, items, rules[name], 1) for name, items in by_group.items()}
    tests = max(1, sum(1 for group in first.values() if group.record.mature))
    groups = {name: _group(name, items, rules[name], tests) for name, items in by_group.items()}
    calibration = {
        name.removeprefix(SEGMENT): _calibration(
            name.removeprefix(SEGMENT), items, spec, rules[name]
        )
        for name, items in by_group.items()
        if name.startswith(SEGMENT)
    }
    not_evaluable: dict[str, int] = {}
    for item in graded:
        if not item.resolved and not item.path.open:
            reason = item.graded.reason or "unknown"
            not_evaluable[reason] = not_evaluable.get(reason, 0) + 1
    written = ledger_repo.called_theses(store)
    unwritten = sum(
        1 for thesis in thesis_repo.theses(store, as_of=as_of) if thesis.thesis_id not in written
    )
    return LedgerBook(
        as_of=as_of,
        groups=dict(sorted(groups.items())),
        calibration=dict(sorted(calibration.items())),
        tests=tests,
        not_evaluable=not_evaluable,
        overlapping=overlapping,
        unwritten=unwritten,
        minimum=f"{spec.statistic.min_cohorts} weeks and {spec.statistic.min_effective}",
        calibration_minimum=(
            f"{spec.calibration.min_cohorts} weeks and {spec.calibration.min_effective}"
        ),
        graded=graded,
    )


def record_segments(store: Store, book: LedgerBook, *, at: datetime) -> int:
    """Store each segment's calibration for the book's day; the count newly stored."""
    return sum(
        1
        for calibration in book.calibration.values()
        if ledger_repo.record_segment(store, calibration.stored(book.as_of), at=at)
    )
