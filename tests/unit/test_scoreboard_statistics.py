"""The scoreboard's statistics, hand-computed: Student's t, the alpha spending and its false-alarm
rate over repeated monthly looks, effective observations, Newey-West, Brier and reliability,
calibration and the empirical-Bayes weights (research/preregistrations/scoreboard.md).

Ported in spirit from legacy test_scoreboard_statistics.py and test_ai_scoreboard.py: the minimum
is counted in effective observations, never in predictions.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import itertools
import math
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from scipy import stats

from obliskiq.domain.orders import ExecutionMode
from obliskiq.quant.inference import (
    MeanEstimate,
    regularized_beta,
    student_t_cdf,
    student_t_quantile,
)
from obliskiq.research.prereg import ScoreboardPrereg, ScoreboardSpec, load_any
from obliskiq.scoreboard.calibration import (
    CalibrationState,
    calibration_state,
    component_weights,
    graded_effective,
)
from obliskiq.scoreboard.grading import (
    PENDING,
    GradedCall,
    Record,
    dedupe,
    effective_observations,
    grade_call,
    iso_week,
    panel_record,
    probability_score,
    record,
    reliability,
)
from obliskiq.scoreboard.monitor import (
    Backtest,
    gap_look,
    is_look,
    look_alpha,
    months_since,
    spent_by,
    twin_look,
)
from obliskiq.scoreboard.rules import GradingRules
from obliskiq.store.scoreboard import Call, LayerMonth

MONDAY = datetime(2026, 1, 5, 15, tzinfo=UTC)


@pytest.fixture(scope="module")
def spec(repo_root: Path) -> ScoreboardSpec:
    found = load_any(repo_root / "research" / "preregistrations" / "scoreboard.md")
    assert isinstance(found, ScoreboardPrereg)
    return found.spec


@pytest.fixture(scope="module")
def rules(spec: ScoreboardSpec) -> GradingRules:
    return GradingRules.of(spec, target_power=0.8)


def call(
    week: int,
    conid: int = 1,
    *,
    layer: str = "layer",
    direction: str = "up",
    probability: float | None = None,
    base_rate: float | None = None,
    expected: float | None = None,
    baseline: float | None = None,
    hours: int = 0,
) -> Call:
    return Call(
        layer=layer,
        conid=conid,
        direction=direction,  # type: ignore[arg-type]
        called_at=MONDAY + timedelta(weeks=week, hours=hours),
        detail="test",
        probability=probability,
        base_rate=base_rate,
        expected_excess=expected,
        baseline=baseline,
    )


def graded(item: Call, excess: float | None, reason: str | None = None) -> GradedCall:
    return GradedCall(item, iso_week(item.called_at), excess, reason)


# ── Student's t ──


@pytest.mark.parametrize(
    ("months", "alpha", "expected"),
    [
        (6, 0.005, 4.032),
        (12, 0.005, 3.106),
        (60, 0.005, 2.662),
        (6, 0.01, 3.365),
        (60, 0.01, 2.391),
    ],
)
def test_t_quantiles_match_h13_s_locked_table(months: int, alpha: float, expected: float) -> None:
    assert round(student_t_quantile(1 - alpha, months - 1), 3) == expected


@pytest.mark.parametrize("df", [1, 2.5, 7, 30, 500])
@pytest.mark.parametrize("p", [0.0001, 0.05, 0.5, 0.9, 0.999])
def test_t_matches_scipy(df: float, p: float) -> None:
    assert student_t_quantile(p, df) == pytest.approx(stats.t.ppf(p, df), rel=1e-9, abs=1e-9)
    assert student_t_cdf(1.3, df) == pytest.approx(stats.t.cdf(1.3, df), rel=1e-10)
    assert student_t_cdf(-1.3, df) == pytest.approx(stats.t.cdf(-1.3, df), rel=1e-10)


def test_the_incomplete_beta_at_its_ends_and_against_scipy() -> None:
    assert regularized_beta(2, 3, 0.0) == 0.0
    assert regularized_beta(2, 3, 1.0) == 1.0
    assert regularized_beta(2, 3, 0.4) == pytest.approx(stats.beta.cdf(0.4, 2, 3), rel=1e-12)
    assert regularized_beta(2, 3, 0.9) == pytest.approx(stats.beta.cdf(0.9, 2, 3), rel=1e-12)


@pytest.mark.parametrize(
    ("call_", "message"),
    [
        (lambda: regularized_beta(0, 1, 0.5), "needs"),
        (lambda: regularized_beta(1, 1, 1.5), "needs"),
        (lambda: student_t_cdf(1, 0), "positive"),
        (lambda: student_t_quantile(0, 3), "strictly"),
        (lambda: student_t_quantile(1, 3), "strictly"),
    ],
)
def test_t_refuses_what_is_not_a_distribution(call_: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        call_()  # type: ignore[operator]


def test_a_continued_fraction_that_does_not_converge_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from obliskiq.quant import inference

    monkeypatch.setattr(inference, "_BETA_MAX_TERMS", 1)
    with pytest.raises(ArithmeticError):
        inference.regularized_beta(50, 50, 0.5)


# ── alpha spending ──


def test_the_spending_function_is_pocock_type_and_spends_alpha_by_the_last_month(
    spec: ScoreboardSpec,
) -> None:
    monitoring = spec.monitoring
    assert spent_by(0.05, 0) == 0
    assert spent_by(0.05, 1) == pytest.approx(0.05)
    assert spent_by(0.05, 0.5) == pytest.approx(0.05 * math.log(1 + (math.e - 1) / 2))
    looks = range(monitoring.first_look_month, monitoring.planned_months + 1)
    spent = [look_alpha(0.05, m, monitoring) for m in looks]
    assert sum(spent) == pytest.approx(0.05)
    assert spent[0] == pytest.approx(spent_by(0.05, 3 / 120))
    assert all(a > b for a, b in itertools.pairwise(spent))
    assert months_since("2026-01", "2026-01") == 1
    assert months_since("2025-11", "2026-02") == 4
    assert not is_look(2, monitoring)
    assert is_look(3, monitoring)
    assert not is_look(121, monitoring)
    with pytest.raises(ValueError, match="not a look"):
        look_alpha(0.05, 2, monitoring)
    with pytest.raises(ValueError, match="information fraction"):
        spent_by(0.05, 1.5)


def _months(gaps: list[float], twin: list[float] | None = None) -> list[LayerMonth]:
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        LayerMonth(
            layer="x",
            month=f"{2026 + i // 12}-{i % 12 + 1:02d}",
            mode=ExecutionMode.LIVE,
            closed=0,
            live=g,
            twin=0.0 if twin is None else twin[i],
            reference=0.0,
            recorded_at=stamp,
        )
        for i, g in enumerate(gaps)
    ]


def test_a_gap_look_by_hand(spec: ScoreboardSpec) -> None:
    gaps = [-0.01, -0.02, -0.03, -0.02]
    look = gap_look(_months(gaps), spec.monitoring)
    mean, sd = -0.02, math.sqrt(sum((g + 0.02) ** 2 for g in gaps) / 3)
    expected = (mean + 0.001) / (sd / 2)
    alpha = look_alpha(0.10, 4, spec.monitoring)
    assert look.statistic == pytest.approx(expected)
    assert look.critical == pytest.approx(-stats.t.ppf(1 - alpha, 3))
    assert look.spent == pytest.approx(alpha)
    assert look.breached is (expected <= look.critical)


def test_a_gap_look_without_a_month_or_without_variation_is_not_evaluable(
    spec: ScoreboardSpec,
) -> None:
    months = _months([0.0, 0.0, 0.0])
    flat = gap_look(months, spec.monitoring)
    assert not flat.evaluable
    assert not flat.breached
    assert "do not vary" in flat.detail
    missing = [
        *months[:2],
        LayerMonth("x", "2026-03", ExecutionMode.LIVE, 0, None, 0.0, 0.0, MONDAY),
    ]
    assert not gap_look(missing, spec.monitoring).evaluable


def test_a_twin_look_by_hand_and_its_missing_cases(spec: ScoreboardSpec) -> None:
    backtest = Backtest(mean=0.01, sd=0.04, se=0.002)
    months = _months([0.0] * 4, twin=[-0.05, -0.04, -0.06, -0.05])
    look = twin_look(months, backtest, spec.monitoring)
    expected = (-0.20 - 0.04) / math.sqrt(4 * 0.04**2 + 16 * 0.002**2)
    assert look.statistic == pytest.approx(expected)
    alpha = look_alpha(0.05, 4, spec.monitoring)
    assert look.critical == pytest.approx(-stats.norm.ppf(1 - alpha))
    assert not twin_look(months, Backtest(0.0, 0.0, 0.0), spec.monitoring).evaluable
    gone = [*months[:3], LayerMonth("x", "2026-04", ExecutionMode.LIVE, 0, 0.0, 0.0, None, MONDAY)]
    assert not twin_look(gone, backtest, spec.monitoring).evaluable


@pytest.mark.parametrize("boundary", ["gap", "twin"])
def test_repeated_monthly_looks_hold_the_registered_false_alarm_rate(
    spec: ScoreboardSpec, boundary: str
) -> None:
    """Under the null at its edge (a gap of exactly the tolerance; a twin exactly as the
    backtest predicts, with the backtest's mean known), the share of paths with any alarm over
    the 118 looks stays at or below the boundary's alpha."""
    monitoring = spec.monitoring
    rng = random.Random(20261115)
    paths, months = 600, monitoring.planned_months
    tolerance = float(monitoring.gap.tolerance_per_month)
    backtest = Backtest(mean=0.002, sd=0.03, se=0.0)
    alarms = 0
    for _ in range(paths):
        if boundary == "gap":
            values = [rng.gauss(-tolerance, 0.01) for _ in range(months)]
            record_ = _months(values)
        else:
            values = [rng.gauss(backtest.mean, backtest.sd) for _ in range(months)]
            record_ = _months([0.0] * months, twin=values)
        for n in range(monitoring.first_look_month, months + 1):
            look = (
                gap_look(record_[:n], monitoring)
                if boundary == "gap"
                else twin_look(record_[:n], backtest, monitoring)
            )
            if look.breached:
                alarms += 1
                break
    alpha = float(monitoring.gap.alpha if boundary == "gap" else monitoring.twin.alpha)
    # The union bound holds the rate at alpha; three binomial standard errors of sampling slack.
    assert alarms / paths <= alpha + 3 * math.sqrt(alpha * (1 - alpha) / paths)


def test_a_layer_that_lags_its_twin_is_caught(spec: ScoreboardSpec) -> None:
    rng = random.Random(7)
    gaps = [rng.gauss(-0.03, 0.01) for _ in range(12)]
    assert gap_look(_months(gaps), spec.monitoring).breached


# ── effective observations and grading ──


def test_effective_observations_by_hand() -> None:
    # A cohort of 51 at rho 0.25 carries 51 / 13.5; four weekly cohorts graded over four weeks
    # carry one cohort's worth.
    assert effective_observations([51], rho=0.25, horizon_weeks=1) == pytest.approx(51 / 13.5)
    assert effective_observations([51] * 4, rho=0.25, horizon_weeks=4) == pytest.approx(51 / 13.5)
    assert effective_observations([1, 0, 1], rho=0.25, horizon_weeks=2) == pytest.approx(1.0)
    with pytest.raises(ValueError, match="at least a week"):
        effective_observations([1], rho=0.25, horizon_weeks=0)


def test_the_registered_rho_makes_a_year_of_full_panels_about_49_observations(
    rules: GradingRules,
) -> None:
    assert rules.rho == 0.25
    assert rules.horizon_weeks == 4
    year = effective_observations([51] * 52, rho=rules.rho, horizon_weeks=rules.horizon_weeks)
    assert year == pytest.approx(49.1, abs=0.1)


def test_one_call_per_layer_instrument_direction_and_week_the_earliest() -> None:
    first = call(0, hours=0)
    again = call(0, hours=30)
    other_direction = call(0, direction="down", hours=2)
    other_layer = call(0, layer="other", hours=3)
    next_week = call(1)
    kept = dedupe([again, next_week, other_layer, first, other_direction])
    assert first in kept
    assert again not in kept
    assert len(kept) == 4
    assert iso_week(date(2026, 1, 1)) == "2026-W01"


def test_a_call_s_outcome_from_its_marks(rules: GradingRules) -> None:
    prices = {(1, date(2026, 1, 5)): 100.0, (1, date(2026, 2, 2)): 110.0}
    refs = {(9, date(2026, 1, 5)): 50.0, (9, date(2026, 2, 2)): 52.5}

    def mark(conid: int, on: date) -> float | None:
        return prices.get((conid, on)) or refs.get((conid, on))

    up = grade_call(call(0), mark=mark, reference=9, rules=rules, as_of=date(2026, 3, 1))
    assert up.excess == pytest.approx(math.log(1.1) - math.log(1.05))
    assert up.right is True
    assert up.graded
    down = grade_call(
        call(0, direction="down"), mark=mark, reference=9, rules=rules, as_of=date(2026, 3, 1)
    )
    assert down.excess == pytest.approx(-(math.log(1.1) - math.log(1.05)))
    pending = grade_call(call(0), mark=mark, reference=9, rules=rules, as_of=date(2026, 1, 20))
    assert pending.reason == PENDING
    assert pending.right is None
    unmarked = grade_call(
        call(0, conid=2), mark=mark, reference=9, rules=rules, as_of=date(2026, 3, 1)
    )
    assert unmarked.excess is None
    assert "the instrument" in (unmarked.reason or "")
    no_ref = grade_call(
        call(0),
        mark=lambda c, d: prices.get((c, d)),
        reference=9,
        rules=rules,
        as_of=date(2026, 3, 1),
    )
    assert "equity reference" in (no_ref.reason or "")
    zero = grade_call(
        call(0), mark=lambda _c, _d: 0.0, reference=9, rules=rules, as_of=date(2026, 3, 1)
    )
    assert zero.excess is None


def test_newey_west_and_the_pending_verdict_by_hand(rules: GradingRules) -> None:
    # One call a week, outcomes 1, 2, 3, 4 (per cent): too few cohorts for a verdict.
    items = [graded(call(i), v / 100) for i, v in enumerate([1, 2, 3, 4])]
    found = record("layer", items, rules)
    assert found.estimate.mean == pytest.approx(0.025)
    # Four lags on four points: the kernel uses three; floored at the independent variance.
    assert found.estimate.se is not None
    assert found.estimate.se >= math.sqrt(1.25e-4 / 4)
    assert found.decision is None
    assert not found.mature
    assert found.cohorts == 4
    assert found.graded == 4


def test_newey_west_one_lag_by_hand() -> None:
    from obliskiq.research.stats import newey_west

    estimate = newey_west([1, 2, 3, 4], lags=1, alpha=0.05, tests=1, min_points=3)
    assert estimate.mean == 2.5
    assert estimate.se == pytest.approx(0.625)


def _mature(rules: GradingRules, value: float, *, spread: float = 0.01, names: int = 30) -> Record:
    rng = random.Random(3)
    items = [
        graded(call(week, conid=c), value + rng.gauss(0, spread))
        for week in range(40)
        for c in range(names)
    ]
    return record("layer", items, rules)


def test_a_verdict_only_from_the_minimum_in_effective_observations(rules: GradingRules) -> None:
    # 40 weekly cohorts of 30 names: 40 * 30 / (1 + 29 * 0.25) / 4 = 36.4 effective.
    found = _mature(rules, 0.02)
    assert found.effective == pytest.approx(40 * 30 / 8.25 / 4)
    assert found.decision is not None
    assert found.decision.outcome.value == "sufficient"
    # The same count of predictions in few cohorts is not enough: they share the market's move.
    crowded = [graded(call(week, conid=c), 0.02) for week in range(25) for c in range(200)]
    assert record("layer", crowded, rules).decision is None


def test_a_record_counts_pending_and_unmarked_calls_beside_it(rules: GradingRules) -> None:
    items = [
        graded(call(0), None, PENDING),
        graded(call(1), None, "no mark"),
        graded(call(2), 0.01),
    ]
    found = record("layer", items, rules)
    assert (found.calls, found.pending, found.missing, found.graded) == (3, 1, 1, 1)


def test_reliability_and_brier_skill_by_hand(rules: GradingRules) -> None:
    probabilities = [0.9, 0.9, 0.1, 0.1, 0.5]
    outcomes = [True, False, False, False, True]
    bins = reliability(probabilities, outcomes)
    assert [(b.low, b.count, b.observed) for b in bins] == [
        (0.0, 2, 0.0),
        (0.4, 1, 1.0),
        (0.8, 2, 0.5),
    ]
    assert reliability([1.0], [True])[0].high == 1.0
    items = [
        graded(call(i, probability=p), 0.01 if o else -0.01)
        for i, (p, o) in enumerate(zip(probabilities, outcomes, strict=True))
    ]
    score = probability_score(items, rules)
    brier = sum((p - o) ** 2 for p, o in zip(probabilities, outcomes, strict=True)) / 5
    assert score.brier == pytest.approx(brier)
    assert score.reference == pytest.approx(0.4 * 0.6)
    assert score.skill == pytest.approx(1 - brier / 0.24)
    assert score.base == "climatology"
    own = [graded(call(i, probability=0.6, base_rate=0.5), 0.01) for i in range(3)]
    assert probability_score(own, rules).base == "each call's own"
    assert probability_score([], rules).count == 0


# ── calibration ──


def _forecasts(n_weeks: int, names: int, *, skilled: bool, seed: int = 11) -> list[GradedCall]:
    rng = random.Random(seed)
    found: list[GradedCall] = []
    for week in range(n_weeks):
        for c in range(names):
            p = rng.uniform(0.2, 0.8)
            truth = p if skilled else 0.5
            right = rng.random() < truth
            found.append(graded(call(week, conid=c, probability=p), 0.01 if right else -0.01))
    return found


def test_calibration_is_impossible_before_the_minimum(
    rules: GradingRules, spec: ScoreboardSpec
) -> None:
    few = _forecasts(20, 51, skilled=True)
    state = calibration_state("panel", few, rules, spec.calibration)
    assert not state.calibrated
    assert state.shrink == 0.0
    assert "calibration needs" in state.reason
    assert state.p_eff(0.8, 0.5) == 0.5


def test_a_skilled_record_calibrates_and_shrinks_by_its_slope_s_lower_bound(
    rules: GradingRules, spec: ScoreboardSpec
) -> None:
    items = _forecasts(80, 51, skilled=True)
    state = calibration_state("panel", items, rules, spec.calibration)
    assert state.effective >= 50
    assert state.cohorts >= 26
    assert state.calibrated
    assert state.skill is not None
    assert state.skill > 0
    assert state.slope is not None
    assert 0 < state.shrink <= 1
    assert state.shrink < state.slope
    assert state.p_eff(0.7, 0.5) == pytest.approx(0.5 + state.shrink * 0.2)
    with pytest.raises(ValueError, match="probabilities"):
        state.p_eff(1.2, 0.5)


def test_a_record_without_skill_switches_back_to_uncalibrated(
    rules: GradingRules, spec: ScoreboardSpec
) -> None:
    items = _forecasts(80, 51, skilled=False)
    state = calibration_state("panel", items, rules, spec.calibration)
    assert not state.calibrated
    assert "not above zero" in state.reason


def test_a_slope_by_hand(rules: GradingRules, spec: ScoreboardSpec) -> None:
    from obliskiq.scoreboard.calibration import _slope_lower_bound

    items = [
        graded(call(0, probability=0.8, base_rate=0.5), 0.01),
        graded(call(1, probability=0.2, base_rate=0.5), -0.01),
        graded(call(2, probability=0.8, base_rate=0.5), -0.01),
    ]
    slope, lower = _slope_lower_bound(items, effective=3.0, alpha=0.05)
    # x = 0.3, -0.3, 0.3; y = 0.5, -0.5, -0.5: beta = (0.15 + 0.15 - 0.15) / 0.27.
    assert slope == pytest.approx(0.15 / 0.27)
    residual = (
        (0.5 - slope * 0.3) ** 2 + (-0.5 + slope * 0.3) ** 2 + (-0.5 - slope * 0.3) ** 2
    ) / 2
    se = math.sqrt(residual / 0.27)
    assert lower == pytest.approx(slope - stats.norm.ppf(0.95) * se)
    flat = [graded(call(0, probability=0.5, base_rate=0.5), 0.01)] * 2
    assert _slope_lower_bound(flat, effective=2.0, alpha=0.05) == (None, None)


def test_a_record_whose_probabilities_never_differ_from_the_base_rate(
    rules: GradingRules, spec: ScoreboardSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    from obliskiq.scoreboard import calibration

    items = _forecasts(80, 51, skilled=True)
    monkeypatch.setattr(calibration, "_slope_lower_bound", lambda *_a, **_k: (None, None))
    state = calibration.calibration_state("panel", items, rules, spec.calibration)
    assert not state.calibrated
    assert "never differ" in state.reason
    assert graded_effective(items, rules) == pytest.approx(80 * 51 / 13.5 / 4)


def test_a_calibration_state_is_plain_data() -> None:
    state = CalibrationState("x", True, 0.5, 1, 1, 1.0, 0.1, 0.6, "r")
    assert state.p_eff(0.9, 0.5) == pytest.approx(0.7)


# ── empirical-Bayes weights ──


def _record(mean: float, se: float, *, cohorts: int = 30, effective: float = 40.0) -> Record:
    return Record(
        layer="c",
        calls=100,
        pending=0,
        missing=0,
        graded=100,
        cohorts=cohorts,
        effective=effective,
        observed_rho=None,
        estimate=MeanEstimate(100, mean, se, None, None, (mean - se, mean + se)),
        theta=0.005,
        decision=None,
    )


def test_weights_cannot_move_before_the_minimum(spec: ScoreboardSpec) -> None:
    young = {
        "a": _record(0.02, 0.01, cohorts=10),
        "b": _record(0.01, 0.01),
        "c": _record(0.0, 0.01),
    }
    update = component_weights("event", young, spec.weights)
    assert update.weights is None
    assert update.sha256 is None
    assert "2 of 3" in update.reason


def test_weights_by_hand(spec: ScoreboardSpec) -> None:
    components = {"a": _record(0.03, 0.01), "b": _record(0.01, 0.01), "c": _record(-0.02, 0.01)}
    update = component_weights("event", components, spec.weights)
    tau2 = (0.03**2 + 0.01**2 + 0.02**2) / 3 - 0.01**2
    assert update.tau2 == pytest.approx(tau2)
    shrink = tau2 / (tau2 + 0.0001)
    assert update.shrunk["a"] == pytest.approx(0.03 * shrink)
    assert update.weights == pytest.approx({"a": 0.75, "b": 0.25, "c": 0.0})
    assert update.sha256 is not None
    assert len(update.sha256) == 64
    assert '"a":' in update.as_json()


def test_weights_stand_when_no_component_earns_one(spec: ScoreboardSpec) -> None:
    components = {"a": _record(-0.01, 0.01), "b": _record(-0.02, 0.01), "c": _record(0.0, 0.02)}
    update = component_weights("event", components, spec.weights)
    assert update.weights is None
    assert "registration's weights stand" in update.reason
    noise = {"a": _record(0.001, 0.05), "b": _record(-0.001, 0.05), "c": _record(0.0, 0.05)}
    assert component_weights("event", noise, spec.weights).tau2 == 0.0


# ── the panel ──


def test_the_panel_s_ic_increment_spread_and_minimums(
    rules: GradingRules, spec: ScoreboardSpec
) -> None:
    panel_rules = GradingRules.panel(spec, target_power=0.8)
    rng = random.Random(5)
    items: list[GradedCall] = []
    for week in range(40):
        for c in range(40):
            signal = rng.gauss(0, 1)
            outcome = 0.02 * signal + rng.gauss(0, 0.01)
            items.append(
                graded(
                    call(
                        week,
                        conid=c,
                        layer="panel",
                        expected=signal,
                        baseline=rng.gauss(0, 1),
                        probability=0.5,
                    ),
                    outcome,
                )
            )
    young = panel_record("panel", items[:1200], panel_rules, min_names=spec.panel.min_names)
    assert young.effective < 30
    assert young.decision is None
    small = [graded(call(41, conid=c, layer="panel", expected=1.0), 0.0) for c in range(5)]
    found = panel_record("panel", [*items, *small], panel_rules, min_names=spec.panel.min_names)
    assert found.cohorts == 40
    assert found.names == 1600
    assert found.ic.mean is not None
    assert found.ic.mean > 0.8
    assert found.incremental_ic.mean is not None
    assert found.incremental_ic.mean > 0.7
    assert found.spread.mean is not None
    assert found.spread.mean > 0
    assert found.effective == pytest.approx(40 * 40 / 10.75 / 4)
    assert found.decision is not None
    assert found.decision.outcome.value == "sufficient"
    assert found.probabilities.count == 1600


def test_a_cohort_with_one_expected_excess_has_no_decile_spread(spec: ScoreboardSpec) -> None:
    panel_rules = GradingRules.panel(spec, target_power=0.8)
    items = [
        graded(call(0, conid=c, layer="panel", expected=1.0 if c == 0 else None), 0.01 * c)
        for c in range(25)
    ]
    found = panel_record("panel", items, panel_rules, min_names=spec.panel.min_names)
    assert found.cohorts == 1
    assert found.spread.n == 0
