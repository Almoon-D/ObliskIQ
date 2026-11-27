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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final

from ..config.schema import ObliskConfig
from ..domain.analyst import AnalystCall, OpinionScore, ThesisReview, brier
from ..errors import PreregistrationError
from ..quant.costs import CostBook
from ..research.prereg import CalibrationSpec, IdeaLedgerSpec
from ..store import Store, repos
from ..store import analyst as analyst_repo
from ..store import ledger as ledger_repo
from ..store import theses as thesis_repo
from ..store.ledger import IdeaCall, SegmentState
from .calibration import CalibrationState, calibration_state
from .grading import (
    RELIABILITY_BINS,
    ProbabilityScore,
    Record,
    probability_score,
    record,
)
from .h13_run import VenueOf, market_value
from .ledger_path import (
    CallPath,
    CostOf,
    Graded,
    Leg,
    call_path,
    grade,
    round_trip,
)
from .rules import DAYS_PER_WEEK, GradingRules
from .run import StoreMarks

#: The group prefixes of a report.
SEGMENT: Final = "segment:"
SOURCE: Final = "source:"
EVENT: Final = "event:"


def cost_model(
    store: Store, settings: ObliskConfig, spec: IdeaLedgerSpec, venue_of: VenueOf
) -> CostOf:
    """The round trip's cost of a call as a share of its trade (:func:`round_trip`) at the first
    and last close of its path; or why it cannot be priced."""
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
        return round_trip(
            costs,
            symbol=call.symbol,
            venue=venue,
            asset_class=described.asset_class,
            currency=described.currency,
            trade_value=spec.outcome.cost_trade_value,
            rate_to_base=rate,
            buy=Leg(
                found.start,
                found.start_close,
                market_value(store, call.conid, found.start, sessions),
            ),
            sell=Leg(
                found.end, found.end_close, market_value(store, call.conid, found.end, sessions)
            ),
        )

    return cost


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
    #: What each analyst segment spent on its calls in all (USD), every pass counted.
    spend: dict[str, float] = field(default_factory=dict)
    #: The segments' weekly reviews graded against the calls they reviewed (D188).
    reviews: list[OpinionScore] = field(default_factory=list)

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


def review_record(
    reviews: Sequence[tuple[ThesisReview, AnalystCall]], graded: Sequence[Graded]
) -> list[OpinionScore]:
    """The analyst's weekly reviews graded like its second opinions, per segment: the mean Brier
    score of each review whose thesis's idea call resolved at a level or its horizon after the
    review's sheet (a review of a call already resolved, or of an idea never written, is not
    graded)."""
    by_thesis = {item.call.thesis_id: item for item in graded}
    scores: dict[str, list[float]] = {}
    for review, call in reviews:
        item = by_thesis.get(review.thesis_id)
        if item is None or item.path.outcome is None or item.path.end is None:
            continue
        if item.path.end <= call.as_of:
            continue
        reached = item.path.outcome == "target"
        scores.setdefault(call.label, []).append(brier(review.probability, reached))
    return [
        OpinionScore(segment=segment, graded=len(found), brier=sum(found) / len(found))
        for segment, found in sorted(scores.items())
    ]


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
        spend=analyst_repo.label_spend(store, as_of=as_of),
        reviews=review_record(analyst_repo.reviews(store, as_of=as_of), graded),
    )


def record_segments(store: Store, book: LedgerBook, *, at: datetime) -> int:
    """Store each segment's calibration for the book's day; the count newly stored."""
    return sum(
        1
        for calibration in book.calibration.values()
        if ledger_repo.record_segment(store, calibration.stored(book.as_of), at=at)
    )
