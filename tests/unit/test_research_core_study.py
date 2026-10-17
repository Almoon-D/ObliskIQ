"""A toy core study end to end — four candidates against an equity reference, one test each with
Bonferroni across them — and its frontier, the resolution of portfolios the bootstrap cannot tell
apart, the runs before tax, the structure check, the report, the worker processes and the
point-in-time rule. Also the frontier's rule on hand-made draws.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import logging
import math
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from research_core_toy import CORE_TOY, MONTHS, NOW, put_core_toy

from obliskiq.config import ObliskConfig
from obliskiq.domain.research import Observation
from obliskiq.jurisdiction import load_profile
from obliskiq.research.decide import Outcome
from obliskiq.research.prereg import load_prereg
from obliskiq.research.registry import TrialRegistry
from obliskiq.research.report import render, yearly_returns
from obliskiq.research.study import (
    PathTask,
    StrategyStats,
    StudyRunner,
    _Draws,
    _months,
    gather,
    less_costs,
    run_paths,
)
from obliskiq.store import open_research_store

D = Decimal


@pytest.fixture(autouse=True)
def _restore_root_logging():
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def prereg_path(tmp_path: Path) -> Path:
    path = tmp_path / "core_toy.md"
    path.write_text(CORE_TOY, encoding="utf-8")
    return path


def settings_with(settings: ObliskConfig, workers: int) -> ObliskConfig:
    return settings.model_copy(
        update={"research": settings.research.model_copy(update={"workers": workers})}
    )


def run(settings: ObliskConfig, repo_root: Path, prereg_path: Path, directory: Path, **extra):
    runner = StudyRunner(settings, load_profile(repo_root / "config", "es"))
    with open_research_store(settings, directory) as store:
        put_core_toy(store, **extra)
        return runner.run(
            load_prereg(prereg_path), store, TrialRegistry(store), explore=True, lock=None, now=NOW
        )


@pytest.fixture(scope="module")
def result(repo_root, shipped_settings, tmp_path_factory):
    directory = tmp_path_factory.mktemp("core")
    path = directory / "core_toy.md"
    path.write_text(CORE_TOY, encoding="utf-8")
    return run(settings_with(shipped_settings, 1), repo_root, path, directory / "data")


def test_every_candidate_is_one_test_against_the_reference(result):
    assert all(check.passed for check in result.checks), result.checks
    assert [test.name for test in result.tests] == ["fixed", "60/40", "balance", "overlay"]
    assert result.reference.name == "equity"
    reference = result.reference.historical
    assert reference is not None
    for test, stats in zip(result.tests, result.candidates, strict=True):
        assert test.decision.outcome is not Outcome.NOT_EVALUABLE
        run = stats.historical
        assert run is not None
        assert test.estimate == pytest.approx(run.log_growth - reference.log_growth)
        low, high = test.decision.interval
        assert low <= high
        assert test.robustness is not None
        assert test.deflated is not None
    # Before data, sd 0.03 a month over 41 months (0.02 for the overlay): the detectable effect
    # is proportional to it, so the overlay's is two thirds of the others'.
    fixed, overlay = result.tests[0], result.tests[3]
    assert overlay.declared_detectable == pytest.approx(fixed.declared_detectable * 2 / 3)
    assert result.trials == len(result.tests)


def test_the_runs_before_tax_trade_alike_and_pay_none(result):
    for stats in result.portfolios:
        after, before = stats.historical, stats.pre_tax
        assert after is not None and before is not None  # noqa: PT018
        assert before.tax_paid == 0
        assert before.liquidation_value >= after.liquidation_value
    assert (
        result.reference.pre_tax.liquidation_value > result.reference.historical.liquidation_value
    )


def test_static_weights_are_reported_for_the_core_strategies(result):
    weights = {stats.name: stats.static_weights for stats in result.portfolios}
    assert weights["equity"] is None and weights["fixed"] is None  # noqa: PT018
    assert weights["60/40"] == {"EQ": D("0.6"), "BD": D("0.4")}
    assert weights["overlay"] == {"EQ": D(1), "CASH": D(0)}
    assert weights["balance"]["EQ"] == pytest.approx(D(5) / D(21), abs=D("0.000001"))


def test_the_overlay_steps_aside_in_the_fall(result):
    overlay = next(s for s in result.candidates if s.name == "overlay").historical
    held = result.reference.historical
    assert overlay is not None and held is not None  # noqa: PT018
    # The toy equity falls by about a third through 2023; the tilt cuts it to a quarter.
    assert held.max_drawdown < -0.3
    assert overlay.max_drawdown > held.max_drawdown


def test_the_frontier_resolves_each_point_to_a_portfolio_that_respects_it(result):
    names = {stats.name: stats for stats in result.portfolios}
    assert len(result.frontier) == 4  # two limits x two probabilities
    for point in result.frontier:
        for name in point.eligible:
            assert dict(names[name].breach)[point.limit] <= float(point.probability)
        if point.best is None:
            assert point.chosen is None
            continue
        assert point.chosen in {point.best, *point.indistinguishable}
        assert names[point.best].median_log_growth == max(
            names[n].median_log_growth for n in point.eligible
        )
        chosen = names[point.chosen]
        assert all(
            chosen.simplicity <= names[n].simplicity for n in (point.best, *point.indistinguishable)
        )


def test_the_structure_check_runs_the_tilt_on_the_long_market(result):
    structure = result.structure
    assert structure is not None
    assert all(check.passed for check in structure.checks)
    assert structure.first == date(1990, 4, 30)  # four month ends: the first it can evaluate
    assert structure.overlay is not None and structure.held is not None  # noqa: PT018
    assert structure.months_reduced > 0
    assert structure.overlay.max_drawdown > structure.held.max_drawdown
    low, high = structure.interval
    assert low <= high
    assert [p.start for p in structure.subperiods] == [date(1990, 1, 1), date(1995, 1, 1)]


def test_the_report_states_every_section(result):
    text = render(result)
    for fragment in (
        "## Outcomes",
        "| fixed |",
        "## The frontier",
        "owner's authorisation (DECISIONS D60)",
        "## Portfolios after tax",
        "Median minus ln(1 + 30%)",
        "never as a gate",
        "## Portfolios before tax",
        "## Static weights",
        "| EQ | 60.00% |",
        "## Year by year",
        "| 2023 |",
        "Tax paid, by the year it was paid",
        "## Structure check",
        "Descriptive, before tax and never an outcome",
        "past growth after tax is no promise of future superiority",
        "a new kind of crisis",
    ):
        assert fragment in text, fragment


def test_the_bootstrap_does_not_depend_on_the_number_of_workers(
    result, repo_root, shipped_settings, prereg_path, tmp_path
):
    shared = run(settings_with(shipped_settings, 2), repo_root, prereg_path, tmp_path / "data")
    assert shared.tests == result.tests
    assert shared.frontier == result.frontier
    for one, other in zip(shared.portfolios, result.portfolios, strict=True):
        assert (one.median_log_growth, one.breach, one.terminal_wealth_p5) == (
            other.median_log_growth,
            other.breach,
            other.terminal_wealth_p5,
        )


def test_nothing_published_after_the_horizon_changes_any_output(
    result, repo_root, shipped_settings, prereg_path, tmp_path
):
    later = {
        "core.equity": [
            Observation(observed_on=date(2026, 1, 30), available_on=date(2026, 1, 30), value=1.0),
            Observation(observed_on=MONTHS[-1], available_on=date(2026, 2, 2), value=999.0),
            Observation(observed_on=MONTHS[20], available_on=date(2026, 3, 3), value=0.5),
        ]
    }
    perturbed = run(
        settings_with(shipped_settings, 1), repo_root, prereg_path, tmp_path / "data", extra=later
    )
    assert perturbed.tests == result.tests
    assert perturbed.frontier == result.frontier
    assert perturbed.structure == result.structure
    for one, other in zip(perturbed.portfolios, result.portfolios, strict=True):
        assert (one.median_log_growth, one.breach) == (other.median_log_growth, other.breach)


def test_a_worker_runs_its_share_with_the_code_run_in_process(
    repo_root, shipped_settings, prereg_path, tmp_path
):
    prereg = load_prereg(prereg_path)
    profile = load_profile(repo_root / "config", "es")
    with open_research_store(shipped_settings, tmp_path / "data") as store:
        put_core_toy(store)
        inputs = gather(store, prereg, "EUR", 365.25)
    months = _months(inputs.panel, inputs.dates, 7, 7)
    names = ("equity", "overlay")
    paths = np.array([list(range(len(inputs.dates) - 1))])
    task = PathTask(shipped_settings, profile, prereg, months, inputs.dates, names, paths)
    growth, drawdown, wealth, unsold = run_paths(task)
    # The identity path is the history: its growth is the historical run's.
    runner = StudyRunner(shipped_settings, profile)
    history = runner.paths(prereg, months, inputs.dates, names, paths)
    assert growth.tolist() == history[0].tolist()
    assert drawdown.shape == wealth.shape == unsold.shape == (1, 2)
    assert unsold.tolist() == [[0, 0]]
    # Only the month-end rows of the history before the first date are kept for a path.
    prior = months.prior_prices["EQ"]
    assert [row.observed_on for row in prior] == MONTHS[:6]
    assert months.prior_prices["EQ"] is months.prior_prices["EQ_SWAP"]


def test_an_index_proxy_loses_its_vehicles_expense_ratio_day_by_day():
    rows = (
        Observation(observed_on=date(2024, 1, 1), available_on=date(2024, 1, 1), value=100.0),
        Observation(observed_on=date(2025, 1, 1), available_on=date(2025, 1, 1), value=100.0),
    )
    first, second = less_costs(rows, D("0.002"), 366.0)
    assert first.value == 100.0
    assert second.value == pytest.approx(100 * math.exp(-0.002))
    assert less_costs(rows, None, 366.0) is rows


# ── the frontier's rule on hand-made draws ──


def stats(name: str, simplicity: int, median: float, breach: float, traded: float = 0.0):
    from obliskiq.research.simulate import SimulationResult

    run = SimulationResult(
        strategy=name, dates=(date(2020, 1, 31), date(2021, 1, 31)), nav=(100.0, 100.0),
        initial=D(100), liquidation_value=D(100), years=1.0, log_growth=0.0, max_drawdown=0.0,
        costs=D(0), traded=D(repr(traded)), trades=0, taxes=(), withheld=D(0), summaries=(),
        cost_labels=frozenset(),
    )  # fmt: skip
    return StrategyStats(name, simplicity, run, run, median, 1.0, ((D("0.2"), breach),), None)


def test_the_frontier_takes_the_best_unless_a_simpler_one_cannot_be_told_apart(
    prereg_path, repo_root, shipped_settings
):
    prereg = load_prereg(prereg_path)
    spec = prereg.spec.model_copy(
        update={
            "drawdown_limits": [D("0.2")],
            "frontier": prereg.spec.frontier.model_copy(
                update={"breach_probabilities": [D("0.1"), D("0.5")]}
            ),
        }
    )
    prereg = type(prereg)(prereg.path, prereg.sha256, spec, prereg.body)
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 0.01, size=(400, 1))
    growth = np.hstack(
        [
            0.05 + noise,  # simple: rank 1
            0.0505 + noise + rng.normal(0, 0.001, size=(400, 1)),  # close to simple, rank 3
            0.10 + rng.normal(0, 0.001, size=(400, 1)),  # clearly better, rank 2, breaches more
            0.0502 + noise,  # as close, rank 3 too but trades less
        ]
    )
    zero = growth * 0
    draws = _Draws(("simple", "close", "better", "calm"), growth, zero, zero, zero.astype(int))
    portfolio = {
        "simple": stats("simple", 1, 0.05, 0.05),
        "close": stats("close", 3, 0.0505, 0.05, traded=500.0),
        "better": stats("better", 2, 0.10, 0.3),
        "calm": stats("calm", 3, 0.0502, 0.05, traded=1.0),
    }
    tight, loose = StudyRunner._frontier(prereg, portfolio, draws, alpha=0.05, tests=1)
    # At 10 % only the three that breach in 5 % of paths respect the limit; the best of them
    # cannot be told apart from the simplest, which is chosen.
    assert tight.eligible == ("simple", "close", "calm")
    assert (tight.best, tight.chosen) == ("close", "simple")
    assert tight.indistinguishable == ("calm", "simple")
    # At 50 % the clearly better one is eligible and chosen.
    assert (loose.best, loose.chosen, loose.indistinguishable) == ("better", "better", ())
    none = prereg.spec.model_copy(
        update={
            "frontier": prereg.spec.frontier.model_copy(
                update={"breach_probabilities": [D("0.01")]}
            )
        }
    )
    [empty] = StudyRunner._frontier(
        type(prereg)(prereg.path, prereg.sha256, none, prereg.body),
        portfolio, draws, alpha=0.05, tests=1,
    )  # fmt: skip
    assert (empty.eligible, empty.best, empty.chosen) == ((), None, None)
    without = prereg.spec.model_copy(update={"frontier": None})
    assert (
        StudyRunner._frontier(
            type(prereg)(prereg.path, prereg.sha256, without, prereg.body),
            portfolio,
            draws,
            alpha=0.05,
            tests=1,
        )
        == ()
    )


def test_among_equally_simple_portfolios_the_lower_turnover_one_is_chosen(prereg_path):
    prereg = load_prereg(prereg_path)
    spec = prereg.spec.model_copy(
        update={
            "drawdown_limits": [D("0.2")],
            "frontier": prereg.spec.frontier.model_copy(
                update={"breach_probabilities": [D("0.5")]}
            ),
        }
    )
    prereg = type(prereg)(prereg.path, prereg.sha256, spec, prereg.body)
    rng = np.random.default_rng(5)
    noise = rng.normal(0, 0.01, size=(300, 1))
    growth = np.hstack([0.051 + noise, 0.050 + noise + rng.normal(0, 0.005, size=(300, 1))])
    zero = growth * 0
    draws = _Draws(("busy", "calm"), growth, zero, zero, zero.astype(int))
    portfolio = {
        "busy": stats("busy", 2, 0.051, 0.1, traded=900.0),
        "calm": stats("calm", 2, 0.050, 0.1, traded=10.0),
    }
    [point] = StudyRunner._frontier(prereg, portfolio, draws, alpha=0.05, tests=1)
    assert (point.best, point.chosen) == ("busy", "calm")


def test_yearly_returns_run_from_the_initial_capital_to_the_horizon():
    from obliskiq.research.simulate import SimulationResult

    run = SimulationResult(
        strategy="x", dates=(date(2024, 6, 28), date(2024, 12, 31), date(2025, 6, 30)),
        nav=(99.0, 110.0, 121.0), initial=D(100), liquidation_value=D(120), years=1.0,
        log_growth=0.0, max_drawdown=0.0, costs=D(0), traded=D(0), trades=0, taxes=(),
        withheld=D(0), summaries=(), cost_labels=frozenset(),
    )  # fmt: skip
    assert yearly_returns(run) == {2024: pytest.approx(0.10), 2025: pytest.approx(0.10)}
    assert StrategyStats("x", 1, run, None, None, None, (), None).turnover == 0.0
    assert StrategyStats("x", 1, None, None, None, None, (), None).turnover is None
