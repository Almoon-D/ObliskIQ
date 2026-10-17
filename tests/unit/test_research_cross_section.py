"""The cross-section study kind: its pre-registration, the universe and top-group portfolios, the
monthly series and its effect, the adequacy checks (survivorship measured by year), robustness
and the placebo, the command line, the report, and point in time.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import io
import json
import logging
import math
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
import yaml
from research_cross_toy import CROSS, MONTHS, cross_series, member_prices, put_cross_toy
from research_toy import NOW, TOY, git

from obliskiq.cli.app import EXIT_INVALID, EXIT_OK, run
from obliskiq.config import ObliskConfig
from obliskiq.domain.research import Observation
from obliskiq.errors import PreregistrationError, SimulationError
from obliskiq.jurisdiction import load_profile
from obliskiq.research import cross_section_report
from obliskiq.research.cross_section import (
    CrossSectionRunner,
    PlaceboResult,
    SelectionStrategy,
    declared_se,
    excess,
    monthly_returns,
    round_half_up,
    sample_months,
)
from obliskiq.research.decide import Outcome
from obliskiq.research.prereg import (
    CrossSectionPrereg,
    MemberSpec,
    Prereg,
    SelectionSpec,
    SignalSpec,
    front_matter,
    load_any,
    load_prereg,
)
from obliskiq.research.registry import TrialRegistry
from obliskiq.research.simulate import SimulationResult
from obliskiq.store import Store, open_research_store
from obliskiq.store.research import replace_research_series, trials_for

D = Decimal
ONE = Decimal(1)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "research" / "preregistrations").mkdir(parents=True)
    git(root, "init", "-q")
    (root / "research" / "preregistrations" / "toy_cross.md").write_text(CROSS, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "pre-register the toy cross-section")
    return root


@pytest.fixture
def prereg_path(repo: Path) -> Path:
    return repo / "research" / "preregistrations" / "toy_cross.md"


@pytest.fixture
def data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shipped_settings: ObliskConfig
) -> Path:
    directory = tmp_path / "data"
    monkeypatch.setenv("OBLISK_DATA_DIR", str(directory))
    with open_research_store(shipped_settings, directory) as store:
        put_cross_toy(store)
    return directory


@pytest.fixture(autouse=True)
def _restore_root_logging():
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def runner(repo_root: Path, shipped_settings: ObliskConfig) -> CrossSectionRunner:
    return CrossSectionRunner(shipped_settings, load_profile(repo_root / "config", "es"))


def cli(repo_root: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = run(
        ["--config-dir", str(repo_root / "config"), *argv], env_file=None, stdout=out, stderr=err
    )
    return status, out.getvalue(), err.getvalue()


def cross(prereg_path: Path) -> CrossSectionPrereg:
    loaded = load_any(prereg_path)
    assert isinstance(loaded, CrossSectionPrereg)
    return loaded


def edited(tmp_path: Path, change) -> Path:
    """The toy with its front matter changed by ``change`` (a function of the parsed YAML)."""
    data, body = front_matter(CROSS)
    change(data)
    path = tmp_path / "edited.md"
    path.write_text("---\n" + yaml.safe_dump(data, sort_keys=False) + "---\n" + body)
    return path


def run_toy(runner: CrossSectionRunner, prereg: CrossSectionPrereg, store: Store, *, explore):
    return runner.run(prereg, store, TrialRegistry(store), explore=explore, lock=None, now=NOW)


# ── the pre-registration ──


def test_the_cross_section_template_is_a_valid_pre_registration(repo_root: Path):
    template = load_any(repo_root / "research" / "preregistrations" / "TEMPLATE-cross-section.md")
    assert isinstance(template, CrossSectionPrereg)
    assert template.spec.kind == "cross_section"


def test_load_any_reads_either_kind(tmp_path: Path):
    path = tmp_path / "toy.md"
    path.write_text(TOY)
    assert isinstance(load_any(path), Prereg)
    path.write_text(CROSS)
    loaded = load_any(path)
    assert isinstance(loaded, CrossSectionPrereg)
    assert loaded.spec.compared() == ["momentum", "universe", "momentum_stop", "placebo"]
    assert [m.key for m in loaded.spec.study.served()] == ["A", "B", "C", "D"]
    with pytest.raises(PreregistrationError, match="kind"):
        load_prereg(path)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(tests_in_confirmation=1), "tests_in_confirmation is 1"),
        (lambda d: d["tests"][1].update(name="country momentum"), "distinct name"),
        (lambda d: d["tests"][0].update(candidate="nowhere"), "unknown portfolios"),
        (lambda d: d["tests"][0].update(against="momentum"), "compared with itself"),
        (lambda d: d["placebo"].update(test="nothing"), "placebo names no test"),
        (lambda d: d.update(subperiods=[["2022-01-01", "2022-06-30"]]), "not inside"),
        (lambda d: d["study"]["development"].update(end="2023-06-30"), "must end before"),
        (lambda d: d["study"].update(calendar="X"), "not a member with a series"),
        (lambda d: d["study"].update(stop=None), "needs study.stop"),
        (lambda d: d["study"]["members"][1].update(key="A"), "member keys must be unique"),
        (lambda d: d["study"]["signal"].update(skip_months=2), "fewer than the lookback"),
        (lambda d: d["study"]["selection"].update(keep_fraction="0.25"), "at least as long"),
        (lambda d: d["study"]["development"].update(end="2021-12-31"), "must follow"),
    ],
)
def test_a_cross_section_refuses_incoherent_fields(tmp_path: Path, change, message):
    with pytest.raises(PreregistrationError, match=message):
        load_any(edited(tmp_path, change))


@pytest.mark.parametrize(
    ("spans", "message"),
    [
        ([["2022-06-01", "2022-01-01"]], "ends before it starts"),
        ([["2022-01-01", "2022-06-01"], ["2022-03-01", None]], "ordered and disjoint"),
        ([["2022-01-01", None], ["2023-01-01", None]], "only the last span"),
    ],
)
def test_member_spans_are_ordered_disjoint_and_open_only_last(spans, message):
    with pytest.raises(ValueError, match=message):
        MemberSpec(key="Z", name="Z", price_series=None, spans=spans)


def test_a_member_belongs_on_the_days_of_its_spans():
    member = MemberSpec(
        key="Z",
        name="Z",
        price_series=None,
        spans=[(date(2020, 1, 1), date(2020, 6, 30)), (date(2021, 1, 1), None)],
    )
    assert member.member_on(date(2020, 6, 30))
    assert not member.member_on(date(2020, 7, 1))
    assert member.member_on(date(2030, 1, 1))


def test_power_before_data_uses_the_confirmation_months(prereg_path: Path):
    spec = cross(prereg_path).spec
    assert sample_months(spec.study.confirmation) == 23
    test = spec.tests[0]
    assert declared_se(spec, test) == pytest.approx(0.015 * 12 / math.sqrt(23))


# ── the portfolios ──


class View:
    """A market view with stated month-end closes and fresh prices."""

    def __init__(self, closes: dict[str, list[float]], on: date) -> None:
        self.closes = closes
        self.as_of = on

    def month_end_closes(self, key: str):
        return tuple((MONTHS[k], v) for k, v in enumerate(self.closes[key]))

    def close(self, key: str):
        return D(1) if key in self.closes else None


class Held:
    """A book holding ``units`` at the stated weights."""

    def __init__(self, weights: dict[str, Decimal]) -> None:
        self.units = {key: D(1) for key in weights}
        self._weights = weights

    def weight(self, key: str) -> Decimal:
        return self._weights.get(key, D(0))


def member(key: str) -> MemberSpec:
    return MemberSpec(key=key, name=key, price_series=f"s.{key}", spans=[(date(2020, 1, 1), None)])


def strategy(rule: str = "top_group", lag: int = 0, keep: str = "0.75") -> SelectionStrategy:
    return SelectionStrategy(
        name=rule,
        members=tuple(member(k) for k in "ABCD"),
        rule=rule,
        signal=SignalSpec(lookback_months=2, skip_months=1),
        selection=SelectionSpec(enter_fraction=D("0.5"), keep_fraction=D(keep), reset_month=12),
        lag_months=lag,
    )


# Momentum is the change from two month ends back to one: A +30 %, B +20 %, C +10 %, D -10 %.
CLOSES = {
    "A": [100.0, 130.0, 1.0],
    "B": [100.0, 120.0, 1.0],
    "C": [100.0, 110.0, 1.0],
    "D": [100.0, 90.0, 1.0],
}
JUNE = date(2022, 6, 30)


def test_the_top_group_takes_the_best_ranked_from_nothing_held():
    weights = strategy().targets(View(CLOSES, JUNE), Held({}))
    assert weights == {"A": D("0.5"), "B": D("0.5")}


def test_a_held_name_is_kept_while_inside_the_keep_rank():
    # C ranks third, inside the keep rank (3 of 4), so it stays beside A.
    weights = strategy().targets(View(CLOSES, JUNE), Held({"A": D("0.6"), "C": D("0.4")}))
    assert weights == {"A": D("0.6"), "C": D("0.4")}


def test_a_held_name_outside_the_keep_rank_leaves_and_an_entrant_takes_what_is_left():
    weights = strategy().targets(View(CLOSES, JUNE), Held({"A": D("0.55"), "D": D("0.45")}))
    assert weights == {"A": D("0.55"), "B": D("0.45")}


def test_continuing_weights_above_one_are_scaled_to_one():
    weights = strategy().targets(View(CLOSES, JUNE), Held({"A": D("0.7"), "B": D("0.7")}))
    assert weights == {"A": D("0.5"), "B": D("0.5")}


def test_continuing_weights_above_one_leave_nothing_for_an_entrant():
    weights = strategy(keep="0.5").targets(View(CLOSES, JUNE), Held({"A": D("1.2"), "C": D("0.3")}))
    assert weights == {"A": D(1), "B": D(0)}


def test_every_holding_is_reset_to_equal_weights_in_the_reset_month():
    weights = strategy().targets(
        View(CLOSES, date(2022, 12, 30)), Held({"A": D("0.7"), "C": D("0.3")})
    )
    assert weights == {"A": D("0.5"), "C": D("0.5")}


def test_the_universe_holds_every_priced_member():
    closes = {key: CLOSES[key] for key in "ABC"}
    weights = strategy("universe").targets(View(closes, JUNE), Held({}))
    assert set(weights) == {"A", "B", "C"}
    assert sum(weights.values()) <= 1


def test_the_top_group_holds_the_universe_until_a_member_can_be_ranked():
    short = {key: values[-2:] for key, values in CLOSES.items()}
    weights = strategy().targets(View(short, JUNE), Held({}))
    assert set(weights) == set("ABCD")


def test_a_lagged_signal_reads_older_month_ends():
    closes = {key: [*values, 1.0] for key, values in CLOSES.items()}
    assert strategy(lag=1).momentum(View(closes, JUNE), "A") == pytest.approx(0.3)
    assert strategy(lag=2).momentum(View(closes, JUNE), "A") is None


def test_no_member_means_no_holding():
    assert strategy().targets(View({}, JUNE), Held({})) == {}


def test_half_rounds_up():
    assert round_half_up(D("1.5")) == 2
    assert round_half_up(D("2.5")) == 3
    assert round_half_up(D("2.4")) == 2


# ── the series ──


def result_with(values: list[float], liquidation: str) -> SimulationResult:
    return SimulationResult(
        strategy="s", dates=tuple(MONTHS[: len(values)]), nav=tuple(values), initial=D(100),
        liquidation_value=D(liquidation), years=1.0, log_growth=None, max_drawdown=0.0,
        costs=D(0), traded=D(0), trades=0, taxes=(), withheld=D(0), summaries=(),
        cost_labels=frozenset(),
    )  # fmt: skip


def test_the_monthly_series_runs_from_the_initial_capital_to_the_liquidation_value():
    returns = monthly_returns(result_with([99.0, 110.0, 120.0], "115"))
    assert returns == pytest.approx((math.log(1.1), math.log(115 / 110)))
    assert sum(returns) == pytest.approx(math.log(1.15))


def test_a_series_that_ends_with_nothing_is_refused():
    with pytest.raises(SimulationError, match="ended with nothing"):
        monthly_returns(result_with([99.0, 110.0], "0"))


# ── the runner ──


def test_a_run_estimates_each_test_from_the_monthly_series(
    runner: CrossSectionRunner, prereg_path: Path, data_dir: Path, shipped_settings: ObliskConfig
):
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(prereg_path), store, explore=False)
    assert all(check.passed for check in result.checks)
    assert result.dates[0] == date(2023, 1, 31)
    assert result.dates[-1] == date(2024, 12, 31)
    momentum = result.portfolio("momentum")
    universe = result.portfolio("universe")
    assert momentum is not None
    assert universe is not None
    series = excess(momentum, universe)
    assert len(series) == len(result.dates) - 1
    test = result.tests[0]
    assert test.estimate == pytest.approx(12 * float(np.mean(series)))
    # The sums of the monthly series are the log growth of the liquidated accounts.
    total = math.log(float(momentum.after_tax.liquidation_value) / 100000) - math.log(
        float(universe.after_tax.liquidation_value) / 100000
    )
    assert sum(series) == pytest.approx(total)
    assert test.decision.interval is not None
    low, high = test.decision.interval
    assert low <= test.estimate <= high
    assert test.newey_west is not None
    assert test.newey_west.mean == pytest.approx(np.mean(series))
    # A rising leader beats the equal-weighted universe in the toy, after tax.
    assert test.decision.outcome is Outcome.SUFFICIENT
    assert test.pre_tax_estimate is not None
    assert test.pre_tax_estimate > test.estimate
    assert test.robustness is not None
    assert all(part.sign_holds for part in test.robustness.subperiods)
    assert test.robustness.breakeven.status == "above_search"
    assert test.deflated is not None
    assert result.trials == 2
    assert result.placebo is not None
    assert result.placebo.estimate is not None
    assert result.lost_by_year[0].lost == ()


def test_identical_portfolios_show_no_effect_and_no_breakeven(
    runner: CrossSectionRunner, prereg_path: Path, data_dir: Path, shipped_settings: ObliskConfig
):
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(prereg_path), store, explore=False)
    stop = result.tests[1]
    # The toy's leaders never fall far enough to reach the stop.
    assert stop.estimate == 0
    assert stop.decision.outcome is Outcome.INSUFFICIENT
    assert stop.robustness is not None
    assert stop.robustness.breakeven.status == "below_theta_without_costs"
    assert not stop.robustness.breakeven_exceeds_uncertainty
    assert stop.deflated is None


def test_a_leader_that_crashes_is_stopped_out(
    runner: CrossSectionRunner, prereg_path: Path, shipped_settings: ObliskConfig, tmp_path: Path
):
    crash = [
        Observation(observed_on=date(2023, 6, 15), available_on=date(2023, 6, 15), value=50.0),
    ]
    with open_research_store(shipped_settings, tmp_path / "crash") as store:
        put_cross_toy(store)
        a = [
            Observation(observed_on=d, available_on=d, value=p)
            for d, p in zip(MONTHS, member_prices("A"), strict=True)
        ]
        replace_research_series(store, cross_series("A"), [*a, *crash])
        result = run_toy(runner, cross(prereg_path), store, explore=False)
    stopped = result.portfolio("momentum_stop")
    assert stopped is not None
    assert stopped.after_tax.stops == 1
    assert result.tests[1].estimate != 0


def test_exploration_runs_on_the_development_sample(
    runner: CrossSectionRunner, prereg_path: Path, data_dir: Path, shipped_settings: ObliskConfig
):
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(prereg_path), store, explore=True)
    assert result.sample.start == date(2022, 1, 1)
    assert result.dates[-1] == date(2022, 12, 30)
    # X is a member until March 2022 and the vendor does not serve it: one of five.
    first = result.lost_by_year[0]
    assert (first.year, first.members, first.lost, first.share) == (2022, 5, ("X",), 0.2)
    survivorship = next(c for c in result.checks if c.name == "survivorship")
    assert survivorship.passed
    assert "20.0%" in survivorship.detail


def test_survivorship_above_its_maximum_is_not_evaluable(
    runner: CrossSectionRunner, data_dir: Path, shipped_settings: ObliskConfig, tmp_path: Path
):
    path = edited(tmp_path, lambda d: d["adequacy"].update(max_lost_share="0.1"))
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(path), store, explore=True)
    check = next(c for c in result.checks if c.name == "survivorship")
    assert not check.passed
    assert "exceeded in 2022" in check.detail
    assert all(t.decision.outcome is Outcome.NOT_EVALUABLE for t in result.tests)
    assert result.portfolios == ()
    assert result.placebo is not None
    assert result.placebo.fails is None
    report = cross_section_report.render(result)
    assert "Not run." in report
    assert "not run (the outcome is not evaluable)" in report


def test_missing_series_short_samples_and_gaps_are_not_evaluable(
    runner: CrossSectionRunner, shipped_settings: ObliskConfig, tmp_path: Path
):
    path = edited(
        tmp_path,
        lambda d: d["adequacy"].update(min_coverage="1", min_effective_observations=30),
    )
    with open_research_store(shipped_settings, tmp_path / "gappy") as store:
        put_cross_toy(store)
        rows = [
            Observation(observed_on=d, available_on=d, value=p)
            for d, p in zip(MONTHS, member_prices("D"), strict=True)
            if d.year != 2024
        ]
        replace_research_series(store, cross_series("D"), rows)
        result = run_toy(runner, cross(path), store, explore=False)
    failed = {c.name for c in result.checks if not c.passed}
    assert failed == {"coverage", "effective observations"}
    with open_research_store(shipped_settings, tmp_path / "empty") as store:
        result = run_toy(runner, cross(path), store, explore=False)
    missing = next(c for c in result.checks if c.name == "data loaded")
    assert not missing.passed
    assert "cross.a" in missing.detail
    assert result.coverage is None
    assert "No rebalance dates." in cross_section_report.render(result)


def test_a_member_joining_later_is_held_and_covered_only_from_its_span(
    runner: CrossSectionRunner, data_dir: Path, shipped_settings: ObliskConfig, tmp_path: Path
):
    path = edited(tmp_path, lambda d: d["study"]["members"][3].update(spans=[["2023-06-01", None]]))
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(path), store, explore=False)
    assert result.coverage == 1
    assert all(check.passed for check in result.checks)
    assert result.lost_by_year[0].members == 4


def test_a_series_outside_the_registered_kinds_is_not_evaluable(
    runner: CrossSectionRunner, data_dir: Path, shipped_settings: ObliskConfig, tmp_path: Path
):
    path = edited(tmp_path, lambda d: d["adequacy"].update(point_in_time=["vintages"]))
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(path), store, explore=False)
    check = next(c for c in result.checks if c.name == "point in time")
    assert not check.passed
    assert "cross.a" in check.detail


def test_a_simulation_that_cannot_run_makes_the_test_not_evaluable(
    runner: CrossSectionRunner,
    prereg_path: Path,
    data_dir: Path,
    shipped_settings: ObliskConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    def broken(*_args, **_kwargs):
        raise SimulationError("no price")

    monkeypatch.setattr(runner, "simulate", broken)
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(prereg_path), store, explore=False)
    check = result.checks[-1]
    assert (check.name, check.passed, check.detail) == ("simulation", False, "no price")
    assert all(t.decision.outcome is Outcome.NOT_EVALUABLE for t in result.tests)


def test_costs_too_high_to_trade_count_as_no_effect(
    runner: CrossSectionRunner,
    prereg_path: Path,
    data_dir: Path,
    shipped_settings: ObliskConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    original = runner.simulate

    def costly(spec, panel, dates, names, *, multiplier=ONE, taxed=True):
        if multiplier > D(5):
            raise SimulationError("cannot trade")
        return original(spec, panel, dates, names, multiplier=multiplier, taxed=taxed)

    monkeypatch.setattr(runner, "simulate", costly)
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(prereg_path), store, explore=False)
    breakeven = result.tests[0].robustness.breakeven
    assert breakeven.status == "found"
    assert 5 < breakeven.multiplier <= 20


def test_a_subperiod_without_months_has_no_estimate(
    runner: CrossSectionRunner, data_dir: Path, shipped_settings: ObliskConfig, tmp_path: Path
):
    path = edited(tmp_path, lambda d: d.update(subperiods=[["2023-01-01", "2023-01-15"]]))
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(path), store, explore=False)
    part = result.tests[0].robustness.subperiods[0]
    assert (part.estimate, part.sign_holds) == (None, None)


def test_without_a_placebo_none_is_reported(
    runner: CrossSectionRunner, data_dir: Path, shipped_settings: ObliskConfig, tmp_path: Path
):
    path = edited(tmp_path, lambda d: d.update(placebo=None))
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(path), store, explore=False)
    assert result.placebo is None
    assert result.tests[0].robustness.placebo == "not registered for this test"
    assert "No placebo was registered." in cross_section_report.render(result)


def test_a_placebo_fails_while_below_theta():
    def placebo(interval):
        return PlaceboResult("p", "u", "t", 0.0, interval, 0.02)

    assert placebo((-0.01, 0.05)).fails is True
    assert placebo((0.02, 0.05)).fails is False
    assert placebo(None).fails is None


def test_nothing_published_after_the_sample_changes_the_result(
    runner: CrossSectionRunner, prereg_path: Path, shipped_settings: ObliskConfig, tmp_path: Path
):
    later = date(2025, 1, 31)
    extra = {key: [Observation(observed_on=later, available_on=later, value=1.0)] for key in "ABCD"}
    late = [Observation(observed_on=date(2024, 12, 31), available_on=later, value=1.0)]
    results = []
    for name, rows in (("plain", {}), ("perturbed", {k: [*v, *late] for k, v in extra.items()})):
        with open_research_store(shipped_settings, tmp_path / name) as store:
            put_cross_toy(store, extra=rows)
            results.append(run_toy(runner, cross(prereg_path), store, explore=False))
    plain, perturbed = results
    assert [t.estimate for t in plain.tests] == [t.estimate for t in perturbed.tests]
    assert [t.decision for t in plain.tests] == [t.decision for t in perturbed.tests]
    assert plain.dates == perturbed.dates


def test_runs_are_reproducible(
    runner: CrossSectionRunner, prereg_path: Path, data_dir: Path, shipped_settings: ObliskConfig
):
    with open_research_store(shipped_settings, data_dir) as store:
        first = run_toy(runner, cross(prereg_path), store, explore=False)
        second = run_toy(runner, cross(prereg_path), store, explore=False)
    assert first.inputs_sha256 == second.inputs_sha256
    assert [t.decision.interval for t in first.tests] == [t.decision.interval for t in second.tests]


# ── the command line and the report ──


def test_power_before_data_on_the_command_line(repo_root: Path, prereg_path: Path):
    status, out, _ = cli(repo_root, "research", "power", str(prereg_path))
    assert status == EXIT_OK
    assert "confirmation months     23" in out
    assert "country momentum" in out
    assert "stop" in out


def test_exploration_on_the_command_line_is_budgeted_and_labelled(
    repo_root: Path, prereg_path: Path, data_dir: Path, shipped_settings: ObliskConfig
):
    status, out, _ = cli(repo_root, "research", "run", str(prereg_path), "--explore")
    assert status == EXIT_OK, out
    assert "country momentum:" in out
    assert "report" in out
    reports = list((prereg_path.parents[2] / "research" / "reports").glob("toy_cross-explor*"))
    assert len(reports) == 1
    text = reports[0].read_text()
    assert text.startswith("# A toy country momentum\n\n**EXPLORATORY")
    assert "Sample (development)" in text
    with open_research_store(shipped_settings, data_dir) as store:
        trials = trials_for(store, "toy_cross")
    assert [json.loads(t.parameters)["test"] for t in trials] == ["country momentum", "stop"]
    status, out, _ = cli(repo_root, "research", "run", str(prereg_path), "--explore")
    assert status == EXIT_OK
    status, out, _ = cli(repo_root, "research", "run", str(prereg_path), "--explore")
    assert status == EXIT_INVALID
    assert "budget" in out


def test_the_confirmation_run_on_the_command_line_needs_the_lock(
    repo_root: Path, prereg_path: Path, data_dir: Path
):
    status, out, _ = cli(repo_root, "research", "run", str(prereg_path))
    assert status == EXIT_INVALID
    assert "refused" in out
    head = git(prereg_path.parents[2], "rev-parse", "HEAD")
    status, out, _ = cli(repo_root, "research", "lock", str(prereg_path), "--commit", head)
    assert status == EXIT_OK
    status, out, _ = cli(repo_root, "research", "run", str(prereg_path))
    assert status == EXIT_OK, out
    report = (prereg_path.parents[2] / "research" / "reports" / "toy_cross.md").read_text()
    assert "**Confirmation run.**" in report
    assert f"merge commit `{head}`" in report
    assert "## Robustness" in report
    assert "Placebo:" in report
    status, out, _ = cli(repo_root, "research", "run", str(prereg_path))
    assert status == EXIT_INVALID
    assert "refused" in out


def test_coverage_of_a_cross_section_on_the_command_line(
    repo_root: Path, prereg_path: Path, data_dir: Path
):
    status, out, _ = cli(repo_root, "research", "data", "coverage", "--prereg", str(prereg_path))
    assert status == EXIT_OK
    assert "coverage of 4 series" in out


def test_the_report_states_the_survivorship_and_the_placebo(
    runner: CrossSectionRunner, prereg_path: Path, data_dir: Path, shipped_settings: ObliskConfig
):
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(prereg_path), store, explore=True)
    text = cross_section_report.render(result)
    assert "| 2022 | 5 | X | 20.0% |" in text
    assert "placebo minus universe" in text
    assert "Newey-West" in text
    assert cross_section_report.report_name(result).startswith("toy_cross-exploration-")
    confirmed = replace(result, explore=False)
    assert cross_section_report.report_name(confirmed) == "toy_cross.md"


def test_the_report_marks_a_placebo_that_shows_the_effect(
    runner: CrossSectionRunner, prereg_path: Path, data_dir: Path, shipped_settings: ObliskConfig
):
    with open_research_store(shipped_settings, data_dir) as store:
        result = run_toy(runner, cross(prereg_path), store, explore=False)
    shown = replace(
        result, placebo=PlaceboResult("placebo", "universe", "t", 0.1, (0.05, 0.2), 0.02)
    )
    assert "**shows the effect**" in cross_section_report.render(shown)
    failing = replace(
        result, placebo=PlaceboResult("placebo", "universe", "t", 0.0, (-0.1, 0.1), 0.02)
    )
    assert "fails, as it should" in cross_section_report.render(failing)
