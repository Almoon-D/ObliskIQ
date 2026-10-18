"""A toy total-risk study end to end (DECISIONS D88, D91, D92): two total-risk candidates against a
static risk balance with their declared twins and comparisons, daily marks, the owner's point,
the block rule and its sensitivity, the forecast and long-history checks, the report, the worker
processes, the point-in-time rule, a development window, a missing day, and the command line.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import logging
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from research_risk_toy import DAYS, NOW, PATHS, RISK_TOY, put_risk_toy

from obliskiq.cli.app import EXIT_OK, run
from obliskiq.config import ObliskConfig
from obliskiq.jurisdiction import load_profile
from obliskiq.research.decide import Label, Outcome
from obliskiq.research.prereg import load_prereg
from obliskiq.research.registry import TrialRegistry
from obliskiq.research.report import render
from obliskiq.research.study import StudyRunner, gather
from obliskiq.store import open_research_store

D = Decimal


@pytest.fixture(autouse=True)
def _restore_root_logging():
    yield
    logging.getLogger().handlers.clear()


def one_worker(settings: ObliskConfig, workers: int = 1) -> ObliskConfig:
    return settings.model_copy(
        update={"research": settings.research.model_copy(update={"workers": workers})}
    )


def study_run(settings, repo_root, directory: Path, *, explore=False, text=RISK_TOY, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "risk_toy.md"
    path.write_text(text, encoding="utf-8")
    runner = StudyRunner(settings, load_profile(repo_root / "config", "es"))
    with open_research_store(settings, directory / "data") as store:
        put_risk_toy(store, **extra)
        return runner.run(
            load_prereg(path), store, TrialRegistry(store), explore=explore, lock=None, now=NOW
        )


@pytest.fixture(scope="module")
def result(repo_root, shipped_settings, tmp_path_factory):
    return study_run(one_worker(shipped_settings), repo_root, tmp_path_factory.mktemp("risk"))


def test_the_candidates_and_the_comparisons_are_the_runs_tests(result):
    assert all(check.passed for check in result.checks), result.checks
    assert [t.name for t in result.tests] == ["S1", "S2", "S1 against T1", "S2 against T2"]
    assert [t.comparison for t in result.tests] == [False, False, True, True]
    assert [(t.candidate, t.against) for t in result.tests[2:]] == [("S1", "T1"), ("S2", "T2")]
    assert [s.name for s in result.portfolios] == ["A", "S1", "S2", "T1", "T2"]
    assert result.trials == 4
    for test in result.tests:
        assert test.decision.outcome is not Outcome.NOT_EVALUABLE
        assert test.robustness is not None
        assert test.deflated is not None
        low, high = test.decision.interval
        assert low <= high
    assert result.tests[2].decision.theta == 0.005


def test_daily_marks_find_falls_the_month_ends_miss_and_the_point_is_measured(result):
    for stats in result.portfolios:
        assert stats.marks is not None
        assert stats.historical is not None
        assert stats.marks.worst <= stats.historical.max_drawdown + 1e-12
        assert stats.point is not None
        assert 0 <= stats.point <= 1
        assert [years for years, _ in stats.horizons] == [1, 2, 3]
        assert [p for _, p in stats.horizons] == sorted(p for _, p in stats.horizons)
        assert [limit for limit, _, _ in stats.rates] == [D("0.1"), D("0.3")]
        assert dict(stats.breach)[D("0.3")] == stats.point
    equity = next(s for s in result.portfolios if s.name == "S1")
    assert equity.marks is not None
    assert equity.marks.worst < equity.historical.max_drawdown


def test_the_scaled_candidate_holds_less_equity_than_its_twin_in_the_crash(result):
    def equity_share(name: str, on: date) -> float:
        stats = next(s for s in result.portfolios if s.name == name)
        run = stats.historical
        held = run.holdings[run.dates.index(on)]
        value = held.cash + sum(u * held.prices[k] for k, u in held.units.items())
        return sum(u * held.prices[k] for k, u in held.units.items() if k.startswith("EQ")) / value

    crash = date(2020, 4, 30)
    assert equity_share("S1", crash) < equity_share("T1", crash) - 0.1
    # The twin is static: it holds its declared share, within its bands, through the crash.
    assert abs(equity_share("T1", crash) - 0.625) < 0.05
    twin = next(s for s in result.portfolios if s.name == "T1")
    assert twin.static_weights == {"EQ": D("0.625"), "CASH": D("0.375")}


def test_the_block_rule_the_sensitivity_and_the_checks_are_reported(result):
    rule = result.block_rule
    assert rule is not None
    assert rule.respected
    assert rule.possible
    assert rule.block_months == 6
    assert rule.sample_months == 47
    assert [row.block_months for row in result.sensitivity] == [4, 6, 9]
    for row in result.sensitivity:
        assert [name for name, _ in row.intervals] == [t.name for t in result.tests]
        assert [name for name, _ in row.points] == ["A", "S1", "S2", "T1", "T2"]
    assert [(f.name, f.estimator) for f in result.forecasts] == [("S1", "fast"), ("S2", "blend")]
    daily, monthly = result.long_history
    assert [r.name for r in daily.runs] == ["S1", "S2", "T1", "T2"]
    assert [r.name for r in monthly.runs] == ["S1", "T1"]
    assert monthly.skipped == ("S2", "T2")
    assert monthly.path_months == 36


def test_the_report_states_every_section(result):
    text = render(result)
    for heading in (
        "## Comparisons",
        "## The owner's point",
        "## Block lengths",
        "## Forecast check",
        "## Long-history check",
        "### a toy century",
        "Max drawdown (daily closes)",
        "Max drawdown (month ends)",
        "S1 against T1 | S1 minus T1",
        "the blocks are at least twice it",
        "Daily marks: between two month ends",
    ):
        assert heading in text, heading
    broken = replace(
        result,
        block_rule=replace(result.block_rule, respected=False),
        sensitivity=(),
        forecasts=(),
    )
    text = render(broken)
    assert "**the blocks are shorter than twice it**" in text
    assert "No block-length sensitivity was registered or run." in text
    assert "Not run (see the adequacy checks)." in text
    impossible = render(replace(result, block_rule=replace(result.block_rule, possible=False)))
    assert "no block length can respect it" in impossible
    assert "Each path is 3.91 years long." in render(result)
    assert "drawdown reaches each limit within 3 years:" in render(result)
    short = replace(result.reference, marks=replace(result.reference.marks, years=2.0))
    assert "**shorter than the owner's 3 years**" in render(replace(result, reference=short))


def test_a_worker_process_gives_the_draws_of_the_process(repo_root, shipped_settings, tmp_path):
    settings = one_worker(shipped_settings)
    path = tmp_path / "risk_toy.md"
    path.write_text(RISK_TOY, encoding="utf-8")
    prereg = load_prereg(path)
    profile = load_profile(repo_root / "config", "es")
    with open_research_store(settings, tmp_path / "data") as store:
        put_risk_toy(store)
        inputs = gather(store, prereg, "EUR", 365.25)
    names = ["A", "S1", "T1"]
    alone = StudyRunner(settings, profile)
    shared = StudyRunner(one_worker(shipped_settings, 2), profile)
    daily = alone._daily(prereg, inputs)
    months = alone._months(inputs, daily)
    one = alone._bootstrap(prereg, months, inputs.dates, names, 6, 4)
    two = shared._bootstrap(prereg, months, inputs.dates, names, 6, 4)
    for field in ("growth", "drawdown", "first_hit", "beyond", "episodes", "years"):
        assert np.array_equal(getattr(one, field), getattr(two, field)), field


def test_a_strategy_sees_nothing_after_its_date(repo_root, shipped_settings, tmp_path):
    settings = one_worker(shipped_settings)
    cut = date(2021, 6, 30)
    later = [i for i, day in enumerate(DAYS) if day > cut]
    perturbed = {key: list(values) for key, values in PATHS.items()}
    for i in later:
        perturbed["risk.equity"][i] *= 1.0 + 0.2 * ((i % 7) - 3) / 3
    first = study_run(settings, repo_root, tmp_path / "one")
    second = study_run(settings, repo_root, tmp_path / "two", paths=perturbed)
    for name in ("S1", "S2"):
        a = next(s for s in first.portfolios if s.name == name).historical
        b = next(s for s in second.portfolios if s.name == name).historical
        through = [k for k, day in enumerate(a.dates[:-1]) if day <= cut]
        assert [a.holdings[k] for k in through] == [b.holdings[k] for k in through]


def test_the_development_window_tests_the_comparisons_alone(repo_root, shipped_settings, tmp_path):
    found = study_run(one_worker(shipped_settings), repo_root, tmp_path, explore=True)
    assert found.explore
    assert found.development is not None
    assert [t.name for t in found.tests] == ["S1 against T1", "S2 against T2"]
    assert found.frontier == ()
    assert found.long_history == ()
    assert found.sensitivity == ()
    assert "effective observations" not in [c.name for c in found.checks]
    assert all(check.passed for check in found.checks), found.checks
    assert found.dates[0] == date(2019, 2, 28)
    assert found.dates[-1] == date(2019, 12, 31)
    assert [s.name for s in found.portfolios if s.historical is not None] == [
        "S1", "S2", "T1", "T2",
    ]  # fmt: skip
    assert all(t.decision.outcome is not Outcome.NOT_EVALUABLE for t in found.tests)
    assert found.trials == 2
    text = render(found)
    assert "Development window: 2019-02-01 to 2019-12-31" in text
    assert "Not computed in an exploration on the development window." in text
    assert "## The owner's point" not in text
    assert "S1 against T1: not run in an exploration on the development window." in text
    assert "Not run in an exploration on the development window." in text
    assert "reaches each limit within a path of 10 months (shorter than the owner's" in text


def test_a_day_missing_inside_a_month_makes_the_study_not_evaluable(
    repo_root, shipped_settings, tmp_path
):
    gaps = {key: list(values) for key, values in PATHS.items()}
    # Ten weekdays without a bond price in the middle of March 2022: past the age limit.
    hole = [i for i, day in enumerate(DAYS) if date(2022, 3, 8) <= day <= date(2022, 3, 21)]
    days = [day for i, day in enumerate(DAYS) if i not in hole]

    def put(store, **_: object) -> None:
        put_risk_toy(store)
        from research_risk_toy import rows, toy_series

        from obliskiq.store.research import replace_research_series

        values = [v for i, v in enumerate(gaps["risk.bond"]) if i not in hole]
        replace_research_series(store, toy_series("risk.bond"), rows(days, values))

    path = tmp_path / "risk_toy.md"
    path.write_text(RISK_TOY, encoding="utf-8")
    settings = one_worker(shipped_settings)
    runner = StudyRunner(settings, load_profile(repo_root / "config", "es"))
    with open_research_store(settings, tmp_path / "data") as store:
        put(store)
        found = runner.run(
            load_prereg(path), store, TrialRegistry(store), explore=False, lock=None, now=NOW
        )
    failed = [c for c in found.checks if not c.passed]
    assert [c.name for c in failed] == ["simulation"]
    assert "price is missing on a day between 2022-02-28 and 2022-03-31" in failed[0].detail
    assert all(t.decision.outcome is Outcome.NOT_EVALUABLE for t in found.tests)
    assert found.forecasts == ()
    text = render(found)
    assert "| A | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |" in text
    assert "Each path is as long as the sample." in text


def test_a_day_missing_in_the_development_window_makes_its_run_not_evaluable(
    repo_root, shipped_settings, tmp_path
):
    from research_risk_toy import rows, toy_series

    from obliskiq.store.research import replace_research_series

    hole = [i for i, day in enumerate(DAYS) if date(2019, 6, 3) <= day <= date(2019, 6, 14)]
    path = tmp_path / "risk_toy.md"
    path.write_text(RISK_TOY, encoding="utf-8")
    settings = one_worker(shipped_settings)
    runner = StudyRunner(settings, load_profile(repo_root / "config", "es"))
    with open_research_store(settings, tmp_path / "data") as store:
        put_risk_toy(store)
        values = [v for i, v in enumerate(PATHS["risk.bond"]) if i not in hole]
        days = [day for i, day in enumerate(DAYS) if i not in hole]
        replace_research_series(store, toy_series("risk.bond"), rows(days, values))
        found = runner.run(
            load_prereg(path), store, TrialRegistry(store), explore=True, lock=None, now=NOW
        )
    assert [c.name for c in found.checks if not c.passed] == ["simulation"]
    assert all(t.decision.outcome is Outcome.NOT_EVALUABLE for t in found.tests)


def test_a_development_run_without_its_data_is_not_evaluable(repo_root, shipped_settings, tmp_path):
    text = RISK_TOY.replace("price_series: risk.cash", "price_series: risk.missing")
    found = study_run(one_worker(shipped_settings), repo_root, tmp_path, explore=True, text=text)
    assert not found.checks[0].passed
    assert "never loaded: risk.missing" in found.checks[0].detail
    assert all(t.decision.outcome is Outcome.NOT_EVALUABLE for t in found.tests)


def test_the_roles_a_development_run_reads(tmp_path):
    from obliskiq.research.study import _roles_of

    path = tmp_path / "risk_toy.md"
    path.write_text(RISK_TOY, encoding="utf-8")
    prereg = load_prereg(path)
    study = prereg.spec.study
    assert _roles_of(study.candidates[1], prereg) == {"EQ", "BD", "CASH"}
    assert _roles_of(study.reference, prereg) == {"EQ", "BD", "GD", "CASH"}


def test_the_estimate_needs_a_daily_path_without_gaps(repo_root, shipped_settings, tmp_path):
    from obliskiq.errors import SimulationError
    from obliskiq.quant.covariance import EstimatorRule
    from obliskiq.research.daily import DailyPath
    from obliskiq.research.simulate import Panel, PanelIndex, Pricing, SimulationSettings
    from obliskiq.research.study import _instruments

    path = tmp_path / "risk_toy.md"
    path.write_text(RISK_TOY, encoding="utf-8")
    prereg = load_prereg(path)
    rule = EstimatorRule(half_life=5.0, window=None, ewma_share=1.0, shrinkage=False,
                         prior_observations=1.0)  # fmt: skip
    settings = SimulationSettings("EUR", D(1), D(0), 7, 7, True, 365.25, D(1))
    instruments = {i.key: i for i in _instruments(prereg.spec.study.instruments)}
    on = date(2020, 1, 31)
    bare = Pricing(instruments, PanelIndex(Panel({}, {}, {})), settings)
    with pytest.raises(SimulationError, match="no daily path"):
        bare.covariance(["EQ"], rule, np.eye(1), on)
    returns = np.array([[np.nan, 0.01], [0.01, 0.02], [0.0, -0.01]])
    daily = DailyPath(("EQ", "BD"), (on,), returns, (3,))
    pricing = Pricing(instruments, PanelIndex(Panel({}, {}, {}, daily)), settings)
    with pytest.raises(SimulationError, match="EQ lack a daily price"):
        pricing.covariance(["EQ"], rule, np.eye(1), on)
    first = pricing.covariance(["BD"], rule, np.eye(1) * 1e-4, on)
    other = replace(rule, half_life=2.0)
    second = pricing.covariance(["BD"], other, np.eye(1) * 1e-4, on)
    assert first is not None
    assert second is not None
    assert first.covariance[0, 0] != second.covariance[0, 0]
    assert pricing.covariance(["BD"], rule, np.eye(1) * 1e-4, on) is first


def test_a_long_history_whose_series_are_missing_is_reported_not_run(
    repo_root, shipped_settings, tmp_path
):
    text = RISK_TOY.replace("[century.Market, century.RF]", "[century.Missing, century.RF]")
    found = study_run(one_worker(shipped_settings), repo_root, tmp_path, text=text)
    century = found.long_history[1]
    assert century.runs == ()
    assert not century.checks[0].passed
    assert "never loaded: century.Missing" in century.checks[0].detail
    late = RISK_TOY.replace("    point_in_time: [release_lag]\n", "    point_in_time: [vintages]\n")
    other = study_run(one_worker(shipped_settings), repo_root, tmp_path / "late", text=late)
    assert "not point in time" in other.long_history[1].checks[1].detail
    assert "Not run." in render(other)


def test_a_history_a_rule_cannot_run_on_is_reported_and_the_study_goes_on(
    repo_root, shipped_settings, tmp_path
):
    import research_risk_toy as toy

    from obliskiq.domain.research import PointInTime
    from obliskiq.store.research import replace_research_series

    path = tmp_path / "risk_toy.md"
    path.write_text(RISK_TOY, encoding="utf-8")
    settings = one_worker(shipped_settings)
    runner = StudyRunner(settings, load_profile(repo_root / "config", "es"))
    ruin = list(toy.EXCESS)
    ruin[150] = -1.002  # with the bill, -100 %: the market lost everything that month
    with open_research_store(settings, tmp_path / "data") as store:
        put_risk_toy(store)
        replace_research_series(
            store,
            toy.toy_series("century.Market", kind=PointInTime.RELEASE_LAG),
            toy.rows([toy.last_weekday(d) for d in toy.HISTORY_MONTHS], ruin),
        )
        found = runner.run(
            load_prereg(path), store, TrialRegistry(store), explore=False, lock=None, now=NOW
        )
    assert all(check.passed for check in found.checks)
    century = found.long_history[1]
    assert century.runs == ()
    assert century.checks[-1].name == "simulation"
    assert "lost everything" in century.checks[-1].detail


def test_outside_the_owners_point_a_candidate_cannot_be_sufficient(result):
    from obliskiq.research.decide import decide

    passed = result.checks
    sufficient = decide(interval=(0.02, 0.05), theta=0.01, power=0.9, target_power=0.8,
                        checks=passed, respects_point=False)  # fmt: skip
    assert sufficient.outcome is Outcome.INCONCLUSIVE
    assert sufficient.labels == (Label.POSITIVE, Label.OUTSIDE_POINT)
    assert any("does not respect the owner's point" in line for line in sufficient.cannot_conclude)
    below = decide(interval=(-0.05, -0.02), theta=0.01, power=0.9, target_power=0.8,
                   checks=passed, respects_point=False)  # fmt: skip
    assert below.outcome is Outcome.INSUFFICIENT
    assert below.labels == (Label.NEGATIVE, Label.OUTSIDE_POINT)
    inside = decide(interval=(0.02, 0.05), theta=0.01, power=0.9, target_power=0.8,
                    checks=passed, respects_point=True)  # fmt: skip
    assert inside.outcome is Outcome.SUFFICIENT


def test_research_power_states_each_comparison(repo_root, tmp_path):
    path = tmp_path / "risk_toy.md"
    path.write_text(RISK_TOY, encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    status = run(
        ["--config-dir", str(repo_root / "config"), "research", "power", str(path)],
        env_file=None,
        stdout=out,
        stderr=err,
    )
    assert status == EXIT_OK, err.getvalue()
    text = out.getvalue()
    assert "S1 against T1" in text
    assert "0.50%" in text
    # The comparison table's columns line up whatever the names' lengths.
    table = [line for line in text.splitlines() if "against" in line or "comparison" in line]
    assert len(table) == 3
    assert len({len(line) for line in table}) == 1
