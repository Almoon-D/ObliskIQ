"""Forward grading of every layer's calls (research/preregistrations/scoreboard.md; DECISIONS D62,
D63; ported from legacy ai_scoreboard.py and forecast_panel.py, re-based on the base currency
and the equity reference).

**A call** is a layer's statement about an instrument: a signal, a component, a pattern (in
shadow or active), a thesis on the day it is approved, an LLM nomination, a forecast of the
weekly panel. One call per layer, instrument, direction and ISO week counts: the first, because
a call repeated within the week is the same bet seen again (legacy ``dedupe_observations``).

**Its outcome** over the horizon is the instrument's log return in the base currency less the
equity reference's, signed by direction. Marks are the last close on or before each date, at most
the rules' age, in the base currency. A call whose horizon has not passed is pending; one without
both marks is not graded, counted beside the record and never filled in.

**A layer's record** is the mean of its weekly cohorts' means, with a Newey-West standard error of
as many lags as the horizon has weeks (floored at the independent variance), and its D62 verdict
against θ — only from the minimum of cohorts and effective observations on. A cohort of ``n``
calls carries ``n / (1 + (n - 1) ρ)`` effective observations, and consecutive cohorts share all
but one week of their horizon, so a record carries the sum of those over the horizon's weeks.

**The weekly forecast panel** is graded per cohort by the Spearman IC of its expected excess
against the realised excess, beside the IC of 12-1 momentum and the top-minus-bottom decile
spread; its stated probabilities by Brier against the base rate, with a reliability table.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..quant.inference import MeanEstimate, brier_score, spearman_ic
from ..research.decide import AdequacyCheck, Decision, decide
from ..research.stats import effective_n, newey_west, panel_correlation, power
from ..store.scoreboard import Call
from .rules import GradingRules

#: The Newey-West mean needs at least this many cohorts to report anything.
MIN_SERIES_POINTS = 3
#: A cohort's IC needs this many ranked pairs (quant.inference's floor).
MIN_RANK_PAIRS = 5
#: The reliability table's bins: fifths of the probability scale.
RELIABILITY_BINS = 5
#: Pairs of names whose realised ρ is reported must share this many cohorts.
OBSERVED_RHO_OVERLAP = 3
#: The share of a cohort in each tail of the decile spread.
DECILE = 10

#: The price of an instrument in the base currency at the last close on or before a date, at most
#: an age old; ``None`` when there is none.
type Mark = Callable[[int, date], float | None]


def iso_week(at: datetime | date) -> str:
    """The ``YYYY-Www`` cohort of a stamp (its UTC date) or a date."""
    year, week, _ = (at.date() if isinstance(at, datetime) else at).isocalendar()
    return f"{year}-W{week:02d}"


def dedupe(calls: Iterable[Call]) -> list[Call]:
    """One call per layer, instrument, direction and ISO week: the earliest."""
    seen: dict[tuple[str, int, str, str], Call] = {}
    for call in sorted(calls, key=lambda c: (c.called_at, c.call_id or 0)):
        seen.setdefault((call.layer, call.conid, call.direction, iso_week(call.called_at)), call)
    return list(seen.values())


@dataclass(frozen=True, slots=True)
class GradedCall:
    """A call and what followed it: ``excess`` is the signed excess log return, ``None`` while
    pending or when a mark is missing (``reason`` says which)."""

    call: Call
    week: str
    excess: float | None
    reason: str | None

    @property
    def graded(self) -> bool:
        return self.excess is not None

    @property
    def right(self) -> bool | None:
        """Whether the call was right (its signed excess above zero)."""
        return None if self.excess is None else self.excess > 0


PENDING = "pending: its horizon has not passed"


def grade_call(
    call: Call, *, mark: Mark, reference: int, rules: GradingRules, as_of: date
) -> GradedCall:
    """The outcome of ``call`` from the marks of its instrument and the equity reference."""
    start = call.called_at.date()
    end = start + timedelta(days=rules.horizon_days)
    week = iso_week(call.called_at)
    if end > as_of:
        return GradedCall(call, week, None, PENDING)
    prices = (mark(call.conid, start), mark(call.conid, end))
    references = (mark(reference, start), mark(reference, end))
    if None in prices or None in references:
        missing = "the instrument" if None in prices else "the equity reference"
        return GradedCall(call, week, None, f"no mark of {missing} within the age allowed")
    p0, p1 = prices
    r0, r1 = references
    assert p0 is not None and p1 is not None and r0 is not None and r1 is not None
    if min(p0, p1, r0, r1) <= 0:
        return GradedCall(call, week, None, "a mark that is not a price")
    sign = 1.0 if call.direction == "up" else -1.0
    return GradedCall(call, week, sign * (math.log(p1 / p0) - math.log(r1 / r0)), None)


def effective_observations(sizes: Sequence[int], *, rho: float, horizon_weeks: int) -> float:
    """``Σ n_eff(n_c) / h``: the effective observations of cohorts of ``sizes`` calls, graded over
    ``horizon_weeks`` weeks whose consecutive cohorts overlap."""
    if horizon_weeks < 1:
        raise ValueError(f"a horizon is at least a week, got {horizon_weeks}")
    return sum(effective_n(n, rho) for n in sizes if n > 0) / horizon_weeks


def cohorts(graded: Iterable[GradedCall]) -> dict[str, list[GradedCall]]:
    """The graded calls by ISO week, in week order."""
    found: dict[str, list[GradedCall]] = {}
    for item in sorted(graded, key=lambda g: g.week):
        if item.graded:
            found.setdefault(item.week, []).append(item)
    return found


def observed_rho(by_week: dict[str, list[GradedCall]]) -> float | None:
    """The mean pairwise correlation the graded outcomes show across instruments (reported,
    never decisive)."""
    names = sorted({g.call.conid for items in by_week.values() for g in items})
    if len(names) < 2:
        return None
    panel: list[list[float | None]] = []
    for items in by_week.values():
        row: dict[int, float] = {}
        for item in items:
            if item.excess is not None:
                row.setdefault(item.call.conid, item.excess)
        panel.append([row.get(name) for name in names])
    return panel_correlation(panel, min_overlap=OBSERVED_RHO_OVERLAP)


@dataclass(frozen=True, slots=True)
class Record:
    """A layer's forward record: counts, effective observations, the estimate per horizon with
    its interval and the D62 verdict (``None`` below the minimum: pending, nothing concluded)."""

    layer: str
    calls: int
    pending: int
    missing: int
    graded: int
    cohorts: int
    effective: float
    observed_rho: float | None
    estimate: MeanEstimate
    theta: float
    decision: Decision | None

    @property
    def mature(self) -> bool:
        return self.decision is not None


def _verdict(
    estimate: MeanEstimate, rules: GradingRules, theta: float, cohorts_seen: int, effective: float
) -> Decision | None:
    if cohorts_seen < rules.min_cohorts or effective < rules.min_effective:
        return None
    checks = (
        AdequacyCheck(
            "minimum",
            True,
            f"{cohorts_seen} cohorts (at least {rules.min_cohorts}) and {effective:.1f} "
            f"effective observations (at least {rules.min_effective:g})",
        ),
    )
    interval = estimate.ci
    se = estimate.se
    found_power = (
        power(theta, se, alpha=rules.alpha, tests=1) if se is not None and se > 0 else None
    )
    return decide(
        interval=interval,
        theta=theta,
        power=found_power,
        target_power=rules.target_power,
        checks=checks,
    )


def record(
    layer: str, graded: Sequence[GradedCall], rules: GradingRules, *, theta: float | None = None
) -> Record:
    """The record of ``layer`` from its graded calls, judged against ``theta`` (the rules' θ
    unless the layer's registration states its own)."""
    bound = rules.theta if theta is None else theta
    by_week = cohorts(graded)
    means = [sum(g.excess or 0.0 for g in items) / len(items) for items in by_week.values()]
    estimate = newey_west(
        means,
        lags=rules.horizon_weeks,
        alpha=rules.alpha,
        tests=1,
        min_points=MIN_SERIES_POINTS,
    )
    effective = effective_observations(
        [len(items) for items in by_week.values()],
        rho=rules.rho,
        horizon_weeks=rules.horizon_weeks,
    )
    return Record(
        layer=layer,
        calls=len(graded),
        pending=sum(1 for g in graded if g.reason == PENDING),
        missing=sum(1 for g in graded if not g.graded and g.reason != PENDING),
        graded=sum(1 for g in graded if g.graded),
        cohorts=len(by_week),
        effective=effective,
        observed_rho=observed_rho(by_week),
        estimate=estimate,
        theta=bound,
        decision=_verdict(estimate, rules, bound, len(by_week), effective),
    )


# ── stated probabilities ──


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    low: float
    high: float
    count: int
    mean_stated: float
    observed: float


def reliability(probabilities: Sequence[float], outcomes: Sequence[bool]) -> list[ReliabilityBin]:
    """Stated probabilities by fifths, each beside the frequency observed (legacy
    ``_reliability_bins``); an empty bin is left out."""
    found: list[ReliabilityBin] = []
    edges = [i / RELIABILITY_BINS for i in range(RELIABILITY_BINS + 1)]
    for low, high in zip(edges[:-1], edges[1:], strict=True):
        inside = [
            i
            for i, p in enumerate(probabilities)
            if low <= p < high or (high == 1.0 and p == 1.0)
        ]
        if inside:
            found.append(
                ReliabilityBin(
                    low=low,
                    high=high,
                    count=len(inside),
                    mean_stated=sum(probabilities[i] for i in inside) / len(inside),
                    observed=sum(1 for i in inside if outcomes[i]) / len(inside),
                )
            )
    return found


@dataclass(frozen=True, slots=True)
class ProbabilityScore:
    """A layer's stated probabilities against what happened: Brier, the base rate's Brier and the
    skill between them. ``base`` says which base rate: each call's own, or the climatology."""

    count: int
    cohorts: int
    effective: float
    brier: float | None
    reference: float | None
    skill: float | None
    base: str
    bins: tuple[ReliabilityBin, ...]


def probability_score(graded: Sequence[GradedCall], rules: GradingRules) -> ProbabilityScore:
    """Brier skill of the graded calls that state a probability. The base rate is each call's own
    where every one records it, else the share of them that were right (the climatology, known
    only after the fact, which favours the base rate)."""
    stated = [g for g in graded if g.graded and g.call.probability is not None]
    by_week = cohorts(stated)
    effective = effective_observations(
        [len(items) for items in by_week.values()],
        rho=rules.rho,
        horizon_weeks=rules.horizon_weeks,
    )
    probabilities = [float(g.call.probability or 0.0) for g in stated]
    outcomes = [bool(g.right) for g in stated]
    if not stated:
        return ProbabilityScore(0, 0, 0.0, None, None, None, "none", ())
    ones = [int(o) for o in outcomes]
    brier = brier_score(probabilities, ones)
    if all(g.call.base_rate is not None for g in stated):
        reference = brier_score([float(g.call.base_rate or 0.0) for g in stated], ones)
        base = "each call's own"
    else:
        rate = sum(ones) / len(ones)
        reference = rate * (1 - rate)
        base = "climatology"
    skill = (
        1 - brier / reference
        if brier is not None and reference is not None and reference > 0
        else None
    )
    return ProbabilityScore(
        count=len(stated),
        cohorts=len(by_week),
        effective=effective,
        brier=brier,
        reference=reference,
        skill=skill,
        base=base,
        bins=tuple(reliability(probabilities, outcomes)),
    )


# ── the weekly forecast panel ──


@dataclass(frozen=True, slots=True)
class PanelRecord:
    """The panel's cross-sectional skill: the mean weekly IC with its verdict against θ (an IC),
    momentum's IC and the model's increment over it, the decile spread, and the stated
    probabilities' score. ``names`` is the count of graded forecasts beside ``effective``."""

    layer: str
    names: int
    cohorts: int
    effective: float
    ic: MeanEstimate
    baseline_ic: MeanEstimate
    incremental_ic: MeanEstimate
    spread: MeanEstimate
    decision: Decision | None
    probabilities: ProbabilityScore


def panel_record(
    layer: str, graded: Sequence[GradedCall], rules: GradingRules, *, min_names: int
) -> PanelRecord:
    """The panel's record; a cohort with fewer than ``min_names`` graded forecasts is left out."""
    kept = {
        week: items for week, items in cohorts(graded).items() if len(items) >= min_names
    }
    ic: list[float | None] = []
    baseline: list[float | None] = []
    increment: list[float | None] = []
    spread: list[float | None] = []
    for items in kept.values():
        realised = [g.excess for g in items]
        model = spearman_ic(
            [g.call.expected_excess for g in items], realised, min_pairs=MIN_RANK_PAIRS
        )
        base = spearman_ic([g.call.baseline for g in items], realised, min_pairs=MIN_RANK_PAIRS)
        ic.append(model)
        baseline.append(base)
        increment.append(None if model is None or base is None else model - base)
        ranked = sorted(
            (g.call.expected_excess, g.excess or 0.0)
            for g in items
            if g.call.expected_excess is not None
        )
        tail = max(1, len(ranked) // DECILE)
        if len(ranked) >= 2 * tail:
            top = sum(o for _, o in ranked[-tail:]) / tail
            bottom = sum(o for _, o in ranked[:tail]) / tail
            spread.append(top - bottom)

    def mean(values: list[float | None]) -> MeanEstimate:
        return newey_west(
            values,
            lags=rules.horizon_weeks,
            alpha=rules.alpha,
            tests=1,
            min_points=MIN_SERIES_POINTS,
        )

    estimate = mean(ic)
    effective = effective_observations(
        [len(items) for items in kept.values()], rho=rules.rho, horizon_weeks=rules.horizon_weeks
    )
    graded_ic = sum(1 for value in ic if value is not None)
    return PanelRecord(
        layer=layer,
        names=sum(len(items) for items in kept.values()),
        cohorts=len(kept),
        effective=effective,
        ic=estimate,
        baseline_ic=mean(baseline),
        incremental_ic=mean(increment),
        spread=mean(spread),
        decision=_verdict(estimate, rules, rules.theta, graded_ic, effective),
        probabilities=probability_score(
            [g for items in kept.values() for g in items], rules
        ),
    )
