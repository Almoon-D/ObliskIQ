"""The replay graded under H15 with the idea ledger's grader, and its report.

**A call** is a valid draft of a replayed dossier (recorded refused at no intake). Its outcome
over the registered horizon (``prices.Sessions``) is, for a **stock call**, its log total return
less the log of its cohort's benchmark (the equally weighted gross return of the cohort's
evaluable universe) less the round trip's cost as H14 states it
(``scoreboard.ledger_path.round_trip``, on a trade of the registered value in the base currency);
a call in a cohort not evaluable whole, or whose name has no return over the horizon, is not
evaluable, counted with why. Beside it,
never decisive: the same call over the **equity reference** (in the base currency), which is the
only grading of a call on a universe instrument, and the call **at its own levels** as the
ledger's path reads it on the closes held (``scoreboard.ledger_path.call_path``, ``grade``).

**A segment's record** is the ledger's (``grading.record``): weekly cohorts of the sheet's week,
the mean of their means with a Newey-West standard error of the registered lags, the registered
minimum, and D62's verdict against θ, its interval Bonferroni-adjusted over the segments past the
minimum. The record that counts is the one **after the cutoff**, on the quarters the probe leaves
(a quarter a model of the segment recalls is trimmed); beside it the untrimmed record, the one
before the cutoff, and the **leak measure** (before minus after, with the interval of the
difference at the same level). A segment sufficient before the cutoff and not after is
**remembering**. **Calibration** is the ledger's score (``grading.probability_score``) on the
calls resolved at their levels by the prices' end, against the segment's climatology.

**What a verdict may change**, proposed to the owner and never applied: a segment remembering
or negative after the cutoff loses its forward budget share; the trading segment proposed is
one not remembering and sufficient after, else one not remembering whose calibration is read
with Brier skill above zero, else none (the current one stays).

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from statistics import NormalDist
from typing import Final

from pydantic import ValidationError

from ...config.schema import ObliskConfig
from ...domain.analyst import AnalystCall, DraftOutcome
from ...domain.enums import AssetClass
from ...domain.thesis import ThesisSource
from ...llm.analyst_prompts import STOCK_SECTION, DraftAnswer
from ...quant.costs import CostBook
from ...quant.inference import MeanEstimate
from ...research.decide import Outcome
from ...scoreboard.grading import (
    RELIABILITY_BINS,
    GradedCall,
    ProbabilityScore,
    Record,
    iso_week,
    probability_score,
    record,
)
from ...scoreboard.ledger_path import CallPath, Leg, call_path, grade, round_trip
from ...scoreboard.rules import DAYS_PER_WEEK, GradingRules
from ...store import replay as replay_repo
from ...store import repos
from ...store.db import Store
from ...store.ledger import IdeaCall
from ...store.replay import StoredCohort
from ...store.scoreboard import Call
from ..data import FxRates
from ..prereg_replay import AnalystReplaySpec
from .plan import Dates
from .prices import History, Sessions
from .probe import QuarterRecall, quarter_of, score
from .run import REPLAYED, Plan
from .sources import end_of

USD: Final = "USD"
NOT_EVALUABLE_WHOLE: Final = "its cohort is not evaluable whole"


@dataclass(frozen=True, slots=True)
class ReplayCall:
    """A valid replayed draft, as the grader reads it."""

    label: str
    segment: str
    side: str
    sheet: date
    stock: bool
    symbol: str
    rises: bool
    target: Decimal
    invalidation: Decimal
    horizon: date
    probability: float
    call_id: int


def replay_calls(store: Store, plan: Plan) -> tuple[list[ReplayCall], Counter[str]]:
    """The valid replayed drafts of the registered segments, and each segment's dropped ones."""
    sides = {segment.name: dates for segment, dates in plan.segments}
    found: list[ReplayCall] = []
    dropped: Counter[str] = Counter()
    for draft, call in replay_repo.replay_drafts(store):
        if draft.outcome is DraftOutcome.DROPPED:
            dropped[call.segment or call.model_id] += 1
            continue
        if draft.reason != REPLAYED or call.segment not in sides:
            continue
        try:
            answer = DraftAnswer.model_validate(json.loads(draft.draft))
        except ValidationError, ValueError:  # pragma: no cover - checked when it was written
            continue
        found.append(_call(answer, call, sides[call.segment]))
    return found, dropped


def _call(answer: DraftAnswer, call: AnalystCall, dates: Dates) -> ReplayCall:
    return ReplayCall(
        label=call.label,
        segment=call.segment or call.model_id,
        side=dates.side(call.as_of),
        sheet=call.as_of,
        stock=answer.section == STOCK_SECTION,
        symbol=answer.symbol.strip().upper(),
        rises=answer.target > answer.invalidation,
        target=answer.level("target"),
        invalidation=answer.level("invalidation"),
        horizon=answer.horizon,
        probability=answer.probability,
        call_id=call.call_id,
    )


# ── one call ──


@dataclass(frozen=True, slots=True)
class Graded:
    """A call's outcomes: over the benchmark (stock calls), over the equity reference, and at
    its own levels; each ``GradedCall`` holds its reason where it is not evaluable."""

    call: ReplayCall
    primary: GradedCall | None
    reference: GradedCall
    levels: GradedCall
    path: CallPath | None


class Grader:
    """Grades replayed calls on the replay's store."""

    def __init__(
        self,
        store: Store,
        settings: ObliskConfig,
        spec: AnalystReplaySpec,
        *,
        histories: Mapping[str, History],
        cohorts: Mapping[date, StoredCohort],
        sessions: Sessions,
    ) -> None:
        self._store = store
        self._settings = settings
        self._spec = spec
        self._histories = histories
        self._cohorts = cohorts
        self._sessions = sessions
        self._age = spec.outcome.max_mark_age_days
        self._base = settings.account.base_currency
        self._costs = CostBook.from_config(settings)
        self._reference = settings.portfolio_circuit_breaker.equity_reference_conid
        currencies = {d.currency for d in settings.universe.instruments} | {USD, self._base}
        self._fx = FxRates.from_store(
            store,
            settings.llm.replay.fx_dataset,
            currencies,
            as_of=date.max - timedelta(days=1),
            max_age_days=self._age,
        )
        self._instruments = {
            declared.symbol: declared
            for declared in settings.universe.instruments
            if declared.conid is not None
        }

    # prices in the base currency

    def _to_base(self, currency: str, day: date) -> float | None:
        return self._fx.to_base(1.0, currency, self._base, day)

    def _instrument_close(self, conid: int, day: date) -> tuple[date, float] | None:
        bars = repos.bar_series(self._store, conid, as_of=day, limit=1)
        if not bars or day - bars[-1].session_date > timedelta(days=self._age):
            return None
        return bars[-1].session_date, bars[-1].close

    def _instrument_growth(self, conid: int, currency: str, start: date, end: date) -> float | str:
        first = repos.bar_series(self._store, conid, as_of=start + timedelta(days=self._age))
        later = [bar for bar in first if bar.session_date >= start]
        last = self._instrument_close(conid, end)
        if not later or last is None:
            return "no close of it within the age allowed"
        p0, p1 = later[0].close, last[1]
        fx0, fx1 = self._to_base(currency, later[0].session_date), self._to_base(currency, last[0])
        if fx0 is None or fx1 is None:
            return f"no {currency} rate within the age allowed"
        return (p1 * fx1) / (p0 * fx0)

    def _stock_growth_base(self, symbol: str, start: date, end: date) -> tuple[float, float] | str:
        """A name's total return in dollars and in the base currency."""
        history = self._histories.get(symbol)
        if history is None:
            return "no close at Yahoo"
        growth = history.growth(start, end, max_age_days=self._age)
        if isinstance(growth, str):
            return growth
        fx0, fx1 = self._to_base(USD, start), self._to_base(USD, end)
        if fx0 is None or fx1 is None:
            return "no dollar rate within the age allowed"
        return growth, growth * fx1 / fx0

    # costs

    def _stock_cost(self, symbol: str, start: date, end: date) -> float | str:
        history = self._histories.get(symbol)
        exchange = replay_repo.listing_exchange(self._store, symbol)
        venue = (
            None
            if exchange is None
            else self._settings.sources.stocks.listings.exchanges.get(exchange)
        )
        rate = self._to_base(USD, start)
        if history is None or venue is None or rate is None:
            return f"{symbol} has no venue, close or rate to price its round trip"
        closes = _closes(history, start, end, self._age)
        if closes is None:
            return f"{symbol} has no close at either end"
        sessions = self._settings.thesis.liquidity.sessions
        return round_trip(
            self._costs,
            symbol=symbol,
            venue=venue,
            asset_class=AssetClass.STOCK,
            currency=USD,
            trade_value=self._spec.outcome.cost_trade_value,
            rate_to_base=Decimal(repr(rate)),
            buy=Leg(
                start,
                Decimal(repr(closes[0])),
                replay_repo.average_traded_value(
                    self._store, symbol, before=start, sessions=sessions
                ),
            ),
            sell=Leg(
                end,
                Decimal(repr(closes[1])),
                replay_repo.average_traded_value(
                    self._store, symbol, before=end, sessions=sessions
                ),
            ),
        )

    def _instrument_cost(self, symbol: str, start: date, end: date) -> float | str:
        declared = self._instruments.get(symbol)
        if declared is None or declared.conid is None:
            return f"{symbol} is not a universe instrument"
        exchange = self._settings.ibkr_exchanges.get(declared.listing_exchange)
        rate = self._to_base(declared.currency, start)
        first, last = (
            self._instrument_close(declared.conid, start + timedelta(days=self._age)),
            self._instrument_close(declared.conid, end),
        )
        if exchange is None or rate is None or first is None or last is None:
            return f"{symbol} has no venue, close or rate to price its round trip"
        return round_trip(
            self._costs,
            symbol=symbol,
            venue=exchange.venue,
            asset_class=declared.asset_class,
            currency=declared.currency,
            trade_value=self._spec.outcome.cost_trade_value,
            rate_to_base=Decimal(repr(rate)),
            buy=Leg(start, Decimal(repr(first[1])), None),
            sell=Leg(end, Decimal(repr(last[1])), None),
        )

    # the call

    def grade(self, item: ReplayCall, *, prices_end: date) -> Graded:
        as_call = Call(
            layer=item.label,
            conid=item.call_id,
            direction="up" if item.rises else "down",
            called_at=end_of(item.sheet),
            detail=f"replayed {item.symbol} on {item.sheet.isoformat()}",
            probability=item.probability,
        )
        week = iso_week(item.sheet)

        def missing(reason: str) -> GradedCall:
            return GradedCall(as_call, week, None, reason)

        sign = 1.0 if item.rises else -1.0
        horizon = self._sessions.horizon(item.sheet, self._spec.outcome.horizon_sessions)
        if horizon is None:
            unresolved = missing("its horizon passes the calendar's end")
            return Graded(item, unresolved if item.stock else None, unresolved, unresolved, None)
        start, end = horizon
        reference = self._reference_growth(start, end)
        cost = (
            self._stock_cost(item.symbol, start, end)
            if item.stock
            else self._instrument_cost(item.symbol, start, end)
        )
        if item.stock:
            found = self._stock_growth_base(item.symbol, start, end)
            cohort = self._cohorts.get(item.sheet)
            if isinstance(found, str):
                primary = missing(found)
                over_reference = missing(found)
            else:
                dollars, base = found
                primary = self._primary(as_call, week, dollars, cohort, cost, sign)
                over_reference = _over(as_call, week, base, reference, cost, sign)
        else:
            primary = None
            declared = self._instruments.get(item.symbol)
            growth: float | str = (
                "not a universe instrument"
                if declared is None or declared.conid is None
                else self._instrument_growth(declared.conid, declared.currency, start, end)
            )
            over_reference = (
                missing(growth)
                if isinstance(growth, str)
                else _over(as_call, week, growth, reference, cost, sign)
            )
        levels, path = self._levels(item, as_call, prices_end=prices_end)
        return Graded(item, primary, over_reference, levels, path)

    def _reference_growth(self, start: date, end: date) -> float | str:
        declared = next(
            (d for d in self._settings.universe.instruments if d.conid == self._reference), None
        )
        if declared is None:
            return "the equity reference is not a universe instrument"
        return self._instrument_growth(self._reference, declared.currency, start, end)

    def _primary(
        self,
        as_call: Call,
        week: str,
        growth: float,
        cohort: StoredCohort | None,
        cost: float | str,
        sign: float,
    ) -> GradedCall:
        if cohort is None or cohort.benchmark is None:
            return GradedCall(as_call, week, None, NOT_EVALUABLE_WHOLE)
        if isinstance(cost, str):
            return GradedCall(as_call, week, None, cost)
        excess = sign * (math.log(growth) - math.log(cohort.benchmark)) - cost
        return GradedCall(as_call, week, excess, None)

    def _levels(
        self, item: ReplayCall, as_call: Call, *, prices_end: date
    ) -> tuple[GradedCall, CallPath | None]:
        """The call at its own levels on its closes (the ledger's path and grade), resolved by
        the prices' end; its outcome over the equity reference after costs."""
        idea = IdeaCall(
            thesis_id=item.call_id,
            conid=item.call_id,
            symbol=item.symbol,
            source=ThesisSource.ANALYST,
            segment=item.label,
            event_tags=(),
            rises=item.rises,
            target=item.target,
            invalidation=item.invalidation,
            horizon=item.horizon,
            probability=item.probability,
            known_at=end_of(item.sheet),
        )
        until = min(prices_end, item.horizon)
        if item.stock:
            history = self._histories.get(item.symbol)
            if history is None:
                return GradedCall(as_call, iso_week(item.sheet), None, "no close at Yahoo"), None
            bars = history.bars(item.call_id, after=item.sheet, until=until)
            currency = USD
        else:
            declared = self._instruments.get(item.symbol)
            if declared is None or declared.conid is None:
                return GradedCall(as_call, iso_week(item.sheet), None, "not an instrument"), None
            bars = tuple(
                bar.model_copy(update={"conid": item.call_id})
                for bar in repos.bar_series(
                    self._store, declared.conid, as_of=until, since=item.sheet
                )
                if bar.session_date > item.sheet
            )
            currency = declared.currency
        path = call_path(idea, bars, as_of=prices_end, max_age_days=self._age)
        closes = {bar.session_date: bar.close for bar in bars}

        def price(conid: int, day: date) -> Decimal | None:
            if conid == item.call_id:
                close, rate = closes.get(day), self._to_base(currency, day)
            else:
                found = self._instrument_close(conid, day)
                declared = next(
                    (d for d in self._settings.universe.instruments if d.conid == conid), None
                )
                if found is None or declared is None:
                    return None
                close, rate = found[1], self._to_base(declared.currency, found[0])
            if close is None or rate is None:
                return None
            return Decimal(repr(close * rate))

        def cost(_: IdeaCall, found: CallPath) -> float | str:
            assert found.start is not None
            assert found.end is not None
            return (
                self._stock_cost(item.symbol, found.start, found.end)
                if item.stock
                else self._instrument_cost(item.symbol, found.start, found.end)
            )

        graded = grade(idea, path, price=price, reference=self._reference, cost=cost)
        return graded.graded, path


def _closes(history: History, start: date, end: date, age: int) -> tuple[float, float] | None:
    """The close nearest after ``start`` and the one nearest before ``end``, each within the
    age allowed; ``None`` without both."""
    held = [
        (day, close)
        for day, close in zip(history.sessions, history.closes, strict=True)
        if start <= day <= end
    ]
    allowed = timedelta(days=age)
    if not held or held[0][0] - start > allowed or end - held[-1][0] > allowed:
        return None
    return held[0][1], held[-1][1]


def _over(
    as_call: Call,
    week: str,
    growth: float,
    reference: float | str,
    cost: float | str,
    sign: float,
) -> GradedCall:
    if isinstance(reference, str):
        return GradedCall(as_call, week, None, reference)
    if isinstance(cost, str):
        return GradedCall(as_call, week, None, cost)
    return GradedCall(as_call, week, sign * (math.log(growth) - math.log(reference)) - cost, None)


# ── a segment ──


@dataclass(frozen=True, slots=True)
class Leak:
    """Before minus after, with the interval of the difference at the record's level."""

    difference: float | None
    interval: tuple[float, float] | None


@dataclass(frozen=True, slots=True)
class SegmentResult:
    label: str
    segment: str
    calls: int
    stock_calls: int
    dropped: int
    not_evaluable: Counter[str]
    after: Record
    after_untrimmed: Record
    after_without_best: Record
    before: Record
    over_reference: Record
    leak: Leak
    remembering: bool
    trimmed: tuple[str, ...]
    levels: Counter[str]
    resolved: int
    calibration: ProbabilityScore
    calibration_read: bool
    probe: tuple[QuarterRecall, ...]
    cutoff: date

    @property
    def verdict(self) -> str:
        decision = self.after.decision
        if decision is None:
            return "too few"
        if decision.outcome is Outcome.INSUFFICIENT and decision.interval is not None:
            return "negative" if decision.interval[1] < 0 else "insufficient"
        return decision.outcome.value

    @property
    def negative(self) -> bool:
        return self.verdict == "negative"

    @property
    def sufficient(self) -> bool:
        return self.verdict == Outcome.SUFFICIENT.value

    @property
    def skill(self) -> float | None:
        return self.calibration.skill if self.calibration_read else None


@dataclass(frozen=True, slots=True)
class ReplayBook:
    """Every segment's result and the two proposals, never applied."""

    segments: tuple[SegmentResult, ...]
    tests: int
    loses_share: tuple[str, ...]
    proposed_trading: str | None
    current_trading: str
    spent: float
    units: int
    interrupted: int
    not_replayed: tuple[str, ...] = field(default_factory=tuple)


def replay_rules(
    spec: AnalystReplaySpec, *, target_power: float, calibration: bool = False
) -> GradingRules:
    statistic = spec.statistic
    return GradingRules(
        rho=float(statistic.rho),
        horizon_days=statistic.newey_west_lags * DAYS_PER_WEEK,
        alpha=float(statistic.alpha),
        theta=float(statistic.theta),
        min_cohorts=spec.calibration.min_cohorts if calibration else statistic.min_cohorts,
        min_effective=float(
            spec.calibration.min_effective if calibration else statistic.min_effective
        ),
        max_mark_age_days=spec.outcome.max_mark_age_days,
        target_power=target_power,
    )


def _leak(before: MeanEstimate, after: MeanEstimate, alpha: float, tests: int) -> Leak:
    if before.mean is None or after.mean is None or before.se is None or after.se is None:
        return Leak(None, None)
    difference = before.mean - after.mean
    z = NormalDist().inv_cdf(1 - alpha / tests)
    spread = z * math.sqrt(before.se**2 + after.se**2)
    return Leak(difference, (difference - spread, difference + spread))


def _without_best(graded: Sequence[GradedCall]) -> list[GradedCall]:
    weeks: dict[str, list[float]] = {}
    for g in graded:
        if g.excess is not None:
            weeks.setdefault(g.week, []).append(g.excess)
    if not weeks:
        return list(graded)
    best = max(weeks, key=lambda w: sum(weeks[w]) / len(weeks[w]))
    return [g for g in graded if g.week != best]


def _lower(found: Record) -> float:
    interval = found.estimate.ci
    return -math.inf if interval is None else interval[0]


def grade_replay(
    store: Store,
    settings: ObliskConfig,
    plan: Plan,
    *,
    grader: Grader,
    prices_end: date,
) -> ReplayBook:
    """Every registered segment's replayed calls, graded."""
    spec = plan.spec
    if spec.calibration.reliability_bins != RELIABILITY_BINS:
        raise ValueError(
            f"the replay registers {spec.calibration.reliability_bins} reliability bins; the "
            f"scoreboard's table has {RELIABILITY_BINS}"
        )
    target_power = float(settings.research.decision.target_power)
    rules = replay_rules(spec, target_power=target_power)
    calibration_rules = replay_rules(spec, target_power=target_power, calibration=True)
    calls, dropped = replay_calls(store, plan)
    recalls = score(replay_repo.probes(store))
    alpha = float(spec.probe.alpha)
    graded = [grader.grade(item, prices_end=prices_end) for item in calls]
    by_segment: dict[str, list[Graded]] = {}
    for item in graded:
        by_segment.setdefault(item.call.segment, []).append(item)

    def result(segment_name: str, tests: int) -> SegmentResult:
        segment, dates = next((s, d) for s, d in plan.segments if s.name == segment_name)
        items = by_segment.get(segment_name, [])
        model_ids = {
            settings.llm.models[name].model_id
            for name in (segment.scan_model, segment.dossier_model)
        }
        mine = tuple(r for r in recalls if r.model_id in model_ids)
        trimmed = tuple(
            sorted(
                {
                    r.quarter
                    for r in mine
                    if r.recalled(alpha) and r.quarter >= quarter_of(dates.after[0])
                }
            )
        )
        stock = [g for g in items if g.primary is not None]
        after = [g.primary for g in stock if g.call.side == "after" and g.primary is not None]
        kept = [g for g in after if quarter_of(g.call.called_at.date()) not in trimmed]
        before = [g.primary for g in stock if g.call.side == "before" and g.primary is not None]
        label = items[0].call.label if items else segment_name
        after_record = record(label, kept, rules, tests=tests)
        before_record = record(label, before, rules, tests=tests)
        levels = [g.levels for g in items if g.call.side == "after"]
        resolved = [g for g in levels if g.graded]
        calibration = probability_score(resolved, calibration_rules)
        not_evaluable: Counter[str] = Counter(
            g.primary.reason or "unknown"
            for g in stock
            if g.primary is not None and not g.primary.graded
        )
        before_sufficient = (
            before_record.decision is not None
            and before_record.decision.outcome is Outcome.SUFFICIENT
        )
        after_sufficient = (
            after_record.decision is not None
            and after_record.decision.outcome is Outcome.SUFFICIENT
        )
        return SegmentResult(
            label=label,
            segment=segment_name,
            calls=len(items),
            stock_calls=len(stock),
            dropped=dropped[segment_name],
            not_evaluable=not_evaluable,
            after=after_record,
            after_untrimmed=record(label, after, rules, tests=tests),
            after_without_best=record(label, _without_best(kept), rules, tests=tests),
            before=before_record,
            over_reference=record(
                label,
                [g.reference for g in items if g.call.side == "after"],
                rules,
                tests=tests,
            ),
            leak=_leak(
                before_record.estimate, after_record.estimate, float(spec.statistic.alpha), tests
            ),
            remembering=before_sufficient and not after_sufficient,
            trimmed=trimmed,
            levels=Counter(
                g.path.outcome
                for g in items
                if g.call.side == "after" and g.path is not None and g.path.outcome is not None
            ),
            resolved=len(resolved),
            calibration=calibration,
            calibration_read=calibration.cohorts >= spec.calibration.min_cohorts
            and calibration.effective >= float(spec.calibration.min_effective),
            probe=mine,
            cutoff=dates.cutoff,
        )

    names = [segment.name for segment, _ in plan.segments]
    first = [result(name, 1) for name in names]
    tests = max(1, sum(1 for found in first if found.after.mature))
    segments = tuple(result(name, tests) for name in names)
    loses = tuple(s.segment for s in segments if s.remembering or s.negative)
    sufficient = [s for s in segments if not s.remembering and s.sufficient]
    skilled = [s for s in segments if not s.remembering and (s.skill or 0.0) > 0]
    proposed = (
        max(sufficient, key=lambda s: _lower(s.after)).segment
        if sufficient
        else max(skilled, key=lambda s: s.skill or 0.0).segment
        if skilled
        else None
    )
    units = replay_repo.units(store)
    return ReplayBook(
        segments=segments,
        tests=tests,
        loses_share=loses,
        proposed_trading=proposed,
        current_trading=settings.llm.analyst.trading_segment,
        spent=replay_repo.replay_spend(store),
        units=len(units),
        interrupted=sum(1 for u in units if u.state == "interrupted"),
        not_replayed=tuple(f"{n.model_id} ({n.status})" for n in spec.not_replayed),
    )
